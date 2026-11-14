"""
section_diff.py
日文校對（grammar）以「H2 段落」為單位的處理：切段、把 HTML 換成記號交給 AI、
驗證 AI 回傳、組回 WordPress 的 Gutenberg HTML，以及審查畫面的段落卡片。

（事實查核還是用 html_diff.py 的文字節點切法，跟這支檔案無關。）

── 切段 ──
文章內容是 Gutenberg 區塊（<!-- wp:paragraph --> ... <!-- /wp:paragraph -->）。
先把最外層的區塊依序切出來，再以「最外層的 H2 標題區塊」為界切成段落（section）：
第 0 段是第一個 H2 之前的開頭，之後每個 H2 加上它底下的內容各是一段。

── 段落裡的區塊分類 ──
- para   ：沒有任何樣式的一般段落 <p>。重寫模式下，相鄰的 para 會組成一個「群組」(run)，
           AI 可以在群組內重排句子、合併或拆開段落。
- heading：H2 / H3... 標題。位置固定，只做輕修。
- styled ：帶 class / 區塊屬性的段落（例如外框、置中）。位置固定，只做輕修，避免樣式在合併時遺失。
- fixed  ：圖片、內嵌、Cocoon 方塊、清單等其他區塊。位置固定，裡面的日文文字片段只做輕修。
- gap    ：區塊之間的空白。
圖片等固定區塊會把群組隔開，所以 AI 只能在「兩個固定元素之間」重排文字，
不會把某張圖的說明文字搬到另一張圖下面。

── 記號 ──
段落裡的 <strong>、<a>、<span> 等內嵌標籤換成成對的 ⟦T1⟧…⟦/T1⟧，<br> 這類單獨標籤換成 ⟦T2/⟧，
[簡碼] 換成 ⟦S3⟧。記號編號在同一段落內唯一，所以重寫時記號可以跟著句子在群組內移動。
AI 回傳後檢查記號是否完整、成對、沒有跨段落，不合格的部分一律保留原文。

── 組回 ──
沒有修改（或維持原文）的段落，直接用原始字串，一個字元都不會變；
有修改的部分才局部替換，重寫過的群組重新包成 <!-- wp:paragraph --><p>…</p><!-- /wp:paragraph -->。
"""
import html as html_lib
import re
from difflib import SequenceMatcher

from html_diff import JP_RE, SHORTCODE_RE, count_changes, diff_spans

MODES = ("light", "rewrite")
MODE_LABEL = {"light": "微調", "rewrite": "段落重寫"}  # 跟畫面右上角的模式選擇用字一致

BLOCK_COMMENT_RE = re.compile(r"<!--\s+(/?)wp:([a-z0-9_\-/]+)(\s+\{.*?\})?\s+(/?)-->", re.S)
TAG_RE = re.compile(r"<!--.*?-->|<(/?)([a-zA-Z][a-zA-Z0-9\-]*)\b[^>]*?(/?)>", re.S)
HEADING_RE = re.compile(r"(<h([1-6])\b[^>]*>)(.*?)(</h\2>)", re.S)
PARA_RE = re.compile(r"^(\s*<!--.*?-->\s*)(<p(\s[^>]*)?>)(.*)(</p>)(\s*<!--.*?-->\s*)$", re.S)
TOKEN_RE = re.compile(r"⟦(/?)([TS])(\d+)(/?)⟧")
ANY_BRACKET_TOKEN_RE = re.compile(r"⟦[^⟦⟧]*⟧")

VOID_TAGS = {"br", "img", "hr", "wbr", "input", "source"}
SKIP_TAGS = {"script", "style", "code", "pre", "textarea"}

NEW_PARAGRAPH_TEMPLATE = "<!-- wp:paragraph -->\n<p>{inner}</p>\n<!-- /wp:paragraph -->"
NEW_PARAGRAPH_SEPARATOR = "\n\n"

FIXED_LABELS = {
    "image": "画像",
    "gallery": "画像ギャラリー",
    "jetpack/tiled-gallery": "画像ギャラリー",
    "embed": "埋め込みコンテンツ",
    "html": "HTMLブロック",
    "cocoon-blocks/blogcard": "ブログカード",
    "cocoon-blocks/button-1": "ボタン",
    "separator": "区切り線",
    "table": "表",
    "list": "リスト",
}


# ── 區塊切分 ─────────────────────────────────────────────────────


def split_blocks(content: str):
    """把整篇內容切成最外層的區塊，所有區塊依序串起來會剛好等於原字串。
    回傳 [{"name": 區塊名稱或 None（區塊之間的原始字串）, "attrs": 屬性 JSON 字串, "start", "end"}]
    """
    blocks = []
    pos = 0
    depth = 0
    start = None
    open_name = open_attrs = None
    for m in BLOCK_COMMENT_RE.finditer(content):
        closing = m.group(1) == "/"
        self_closing = m.group(4) == "/"
        name = m.group(2)
        attrs = (m.group(3) or "").strip()
        if depth == 0:
            if closing:
                continue  # 沒有對應開頭的結尾註解，當成一般文字留在原始字串裡
            if m.start() > pos:
                blocks.append({"name": None, "attrs": "", "start": pos, "end": m.start()})
            if self_closing:
                blocks.append({"name": name, "attrs": attrs, "start": m.start(), "end": m.end()})
                pos = m.end()
                continue
            depth = 1
            start = m.start()
            open_name, open_attrs = name, attrs
        else:
            if self_closing:
                continue
            depth += -1 if closing else 1
            if depth == 0:
                blocks.append({"name": open_name, "attrs": open_attrs, "start": start, "end": m.end()})
                pos = m.end()
    # 結尾沒有正確關閉的區塊：從那裡開始到最後都當成原始字串（固定不動）
    if pos < len(content):
        blocks.append({"name": None, "attrs": "", "start": pos, "end": len(content)})
    return blocks


# ── 記號化（內嵌標籤 / 簡碼 → ⟦…⟧） ─────────────────────────────────


class _Counter:
    def __init__(self):
        self.n = 0

    def next(self):
        self.n += 1
        return self.n


def _protect_shortcodes(text: str, counter: _Counter, tokmap: dict) -> str:
    def _repl(m):
        key = f"⟦S{counter.next()}⟧"
        tokmap[key] = m.group(0)
        return key

    return SHORTCODE_RE.sub(_repl, text)


def tokenize_inline(inner: str, counter: _Counter):
    """把一段內嵌 HTML（<p> 或 <hN> 的內容）換成「文字 + 記號」。
    回傳 (記號化文字, 記號對照表)；標籤沒有正確成對、無法安全處理時回傳 None。
    """
    out = []
    tokmap = {}
    stack = []
    pos = 0
    for m in TAG_RE.finditer(inner):
        if m.start() > pos:
            out.append(_protect_shortcodes(inner[pos : m.start()], counter, tokmap))
        tag = m.group(0)
        name = (m.group(2) or "").lower()
        if tag.startswith("<!--") or name in VOID_TAGS or m.group(3) == "/":
            key = f"⟦T{counter.next()}/⟧"
            tokmap[key] = tag
            out.append(key)
        elif m.group(1) == "/":
            if not stack or stack[-1][1] != name:
                return None
            n, _ = stack.pop()
            key = f"⟦/T{n}⟧"
            tokmap[key] = tag
            out.append(key)
        else:
            if name in SKIP_TAGS:
                return None
            n = counter.next()
            stack.append((n, name))
            key = f"⟦T{n}⟧"
            tokmap[key] = tag
            out.append(key)
        pos = m.end()
    if stack:
        return None
    if pos < len(inner):
        out.append(_protect_shortcodes(inner[pos:], counter, tokmap))
    return "".join(out), tokmap


def detokenize(text: str, tokmap: dict) -> str:
    return ANY_BRACKET_TOKEN_RE.sub(lambda m: tokmap.get(m.group(0), m.group(0)), text)


def tokens_in(text: str):
    return ANY_BRACKET_TOKEN_RE.findall(text)


def _nesting_ok(text: str) -> bool:
    """同一段文字裡，⟦Tn⟧ 必須在 ⟦/Tn⟧ 之前，且巢狀順序正確。"""
    stack = []
    for m in TOKEN_RE.finditer(text):
        closing, kind, num, void = m.group(1), m.group(2), m.group(3), m.group(4)
        if kind == "S" or void:
            continue
        if closing:
            if not stack or stack[-1] != num:
                return False
            stack.pop()
        else:
            stack.append(num)
    return not stack


def _plain_from_tokens(text: str, tokmap: dict) -> str:
    """審查畫面用：簡碼還原成原文，格式記號拿掉，HTML 實體轉回文字。"""

    def _repl(m):
        key = m.group(0)
        if key.startswith("⟦S"):
            return tokmap.get(key, "")
        return ""

    return html_lib.unescape(ANY_BRACKET_TOKEN_RE.sub(_repl, text))


# ── 固定區塊裡的文字片段 ───────────────────────────────────────────


def _fixed_text_pieces(raw: str):
    """找出固定區塊（圖片說明、方塊、清單等）裡需要輕修的日文文字片段。
    回傳 [(start, end)]，位置相對於 raw。註解（含 Gutenberg 屬性）與 code/script 內的文字一律跳過；
    文字如果也出現在區塊註解的屬性 JSON 裡（例如 Cocoon 按鈕），改了會讓 Gutenberg 判定區塊損毀，也跳過。
    """
    comments = "".join(re.findall(r"<!--.*?-->", raw, re.S))
    pieces = []
    skip_depth = 0
    pos = 0

    def _consider(s, e):
        text = raw[s:e]
        core = text.strip()
        if skip_depth or not core or not JP_RE.search(core):
            return
        if core in comments or html_lib.unescape(core) in comments:
            return
        lead = len(text) - len(text.lstrip())
        trail = len(text) - len(text.rstrip())
        pieces.append((s + lead, e - trail))

    for m in TAG_RE.finditer(raw):
        if m.start() > pos:
            _consider(pos, m.start())
        name = (m.group(2) or "").lower()
        if name in SKIP_TAGS and not m.group(0).startswith("<!--"):
            if m.group(1) == "/":
                skip_depth = max(0, skip_depth - 1)
            elif m.group(3) != "/":
                skip_depth += 1
        pos = m.end()
    if pos < len(raw):
        _consider(pos, len(raw))
    return pieces


# ── 段落（section）解析 ──────────────────────────────────────────


def _strip_tags_plain(s: str) -> str:
    return html_lib.unescape(re.sub(r"<[^>]+>", "", s)).strip()


def _classify(block: dict, raw: str, counter: _Counter, items: dict, next_item_id):
    """判斷區塊種類並建立需要 AI 處理的項目（item）。會直接修改 block 與 items。"""
    name = block["name"]
    block["items"] = []

    if name is None and not raw.strip():
        block["kind"] = "gap"
        return

    if name == "heading":
        m = HEADING_RE.search(raw)
        if m:
            tok = tokenize_inline(m.group(3), counter)
            block["level"] = int(m.group(2))
            block["heading_text"] = _strip_tags_plain(m.group(3))
            if tok and JP_RE.search(m.group(3)):
                item_id = next_item_id()
                items[item_id] = {
                    "id": item_id,
                    "type": "heading",
                    "text": tok[0],
                    "tokens": tok[1],
                    "span": (m.start(3), m.end(3)),
                    "open_tag": m.group(1),
                    "close_tag": m.group(4),
                }
                block["items"].append(item_id)
                block["kind"] = "heading"
                return
            if tok or not JP_RE.search(m.group(3)):
                block["kind"] = "heading"
                return
        # 標題格式無法處理，退回固定區塊

    if name == "paragraph":
        m = PARA_RE.match(raw)
        if m:
            inner = m.group(4)
            tok = tokenize_inline(inner, counter)
            if tok is not None:
                has_jp = bool(JP_RE.search(inner))
                plain = not block["attrs"] and not m.group(3)
                if not has_jp:
                    block["kind"] = "fixed"  # 沒有日文的段落（例如只有網址）當成固定元素
                    return
                item_id = next_item_id()
                items[item_id] = {
                    "id": item_id,
                    "type": "para" if plain else "text",
                    "text": tok[0],
                    "tokens": tok[1],
                    "span": (m.start(4), m.end(4)),
                    "open_tag": m.group(2),
                    "close_tag": m.group(5),
                }
                block["items"].append(item_id)
                block["kind"] = "para" if plain else "styled"
                return

    block["kind"] = "fixed"
    block["label"] = FIXED_LABELS.get(name or "", name or "HTML")
    for s, e in _fixed_text_pieces(raw):
        tokmap = {}
        text = _protect_shortcodes(raw[s:e], counter, tokmap)
        item_id = next_item_id()
        items[item_id] = {"id": item_id, "type": "text", "text": text, "tokens": tokmap, "span": (s, e)}
        block["items"].append(item_id)


def _build_units(blocks):
    """把區塊串成「單位」：相鄰的 para（中間只隔空白）合成一個 run，其他區塊各自一個單位。"""
    units = []
    run = None
    pending_gaps = []
    run_no = 0

    def _close_run():
        nonlocal run
        if run is not None:
            units.append(run)
            run = None
        units.extend({"type": "block", "block": g} for g in pending_gaps)
        pending_gaps.clear()

    for b in blocks:
        if b["kind"] == "para":
            if run is None:
                run = {"type": "run", "id": f"r{run_no}", "blocks": [b]}
                run_no += 1
            else:
                run["blocks"].extend(pending_gaps)
                pending_gaps.clear()
                run["blocks"].append(b)
        elif b["kind"] == "gap" and run is not None:
            pending_gaps.append(b)
        else:
            _close_run()
            units.append({"type": "block", "block": b})
    _close_run()
    for u in units:
        if u["type"] == "run":
            u["para_ids"] = [b["items"][0] for b in u["blocks"] if b["kind"] == "para"]
            u["start"] = u["blocks"][0]["start"]
            u["end"] = u["blocks"][-1]["end"]
    return units


def parse_sections(content: str):
    """把文章切成 H2 段落。回傳 list，每個元素：
    {"index", "start", "end", "heading", "blocks", "units", "items"}
    start/end 是在整篇 content 裡的位置；所有段落依序串起來等於原文。
    """
    content = content or ""
    blocks = split_blocks(content)
    # 先找出所有 H2 的位置當作切點
    boundaries = []
    for i, b in enumerate(blocks):
        if b["name"] == "heading":
            m = HEADING_RE.search(content, b["start"], b["end"])
            if m and m.group(2) == "2":
                boundaries.append(i)

    groups = []
    if not boundaries or boundaries[0] > 0:
        groups.append(blocks[: boundaries[0]] if boundaries else blocks)
    for k, bi in enumerate(boundaries):
        end = boundaries[k + 1] if k + 1 < len(boundaries) else len(blocks)
        groups.append(blocks[bi:end])
    # 開頭如果只有空白（文章一開始就是 H2），不需要獨立成一段，併進下一段
    if len(groups) > 1 and all(b["name"] is None and not content[b["start"] : b["end"]].strip() for b in groups[0]):
        groups[1] = groups[0] + groups[1]
        groups.pop(0)

    sections = []
    for grp in groups:
        if not grp:
            continue
        counter = _Counter()
        items = {}
        item_no = [0]

        def _next_item_id():
            item_id = f"i{item_no[0]}"
            item_no[0] += 1
            return item_id

        for b in grp:
            raw = content[b["start"] : b["end"]]
            b["raw"] = raw
            _classify(b, raw, counter, items, _next_item_id)

        first = next((b for b in grp if b["kind"] != "gap"), None)
        heading_block = first if first and first["kind"] == "heading" and first.get("level") == 2 else None
        sections.append(
            {
                "start": grp[0]["start"],
                "end": grp[-1]["end"],
                "heading": heading_block["heading_text"] if heading_block else "（開頭）",
                "blocks": grp,
                "units": _build_units(grp),
                "items": items,
            }
        )
    for i, s in enumerate(sections):
        s["index"] = i
    return sections


def section_needs_ai(section) -> bool:
    return bool(section["items"])


# ── 給 AI 的輸入 ─────────────────────────────────────────────────


def ai_payload(section):
    """組成要送給 AI 的段落內容（JSON 可序列化的 list）。"""
    out = []
    items = section["items"]
    for u in section["units"]:
        if u["type"] == "run":
            out.append(
                {
                    "id": u["id"],
                    "type": "paragraphs",
                    "paragraphs": [items[pid]["text"] for pid in u["para_ids"]],
                }
            )
            continue
        b = u["block"]
        if b["kind"] == "gap":
            continue
        if b["kind"] == "heading" and not b["items"]:
            out.append({"type": "fixed", "label": "見出し：" + b.get("heading_text", "")})
            continue
        if b["kind"] == "fixed" and not b["items"]:
            out.append({"type": "fixed", "label": b.get("label", "")})
            continue
        for item_id in b["items"]:
            it = items[item_id]
            out.append({"id": item_id, "type": "heading" if it["type"] == "heading" else "text", "text": it["text"]})
    return out


def result_to_ai_format(result):
    """把存檔用的 result 轉回 AI 輸出的格式，用在「重新調整」時給 AI 看上一次的結果。"""
    if not result:
        return None
    return {
        "items": [{"id": k, "text": v} for k, v in (result.get("items") or {}).items()],
        "groups": [{"id": k, "paragraphs": v} for k, v in (result.get("runs") or {}).items()],
        "note": result.get("note") or "",
    }


# ── 驗證 AI 回傳 ─────────────────────────────────────────────────


# AI 很容易把記號「正規化」成另一個長得很像的字（style_profile 明確禁止），prompt 提醒了也擋不住，
# 所以在程式裡還原：原文只用左邊的寫法、AI 卻換成右邊的寫法時，換回原文的寫法。
SYMBOL_VARIANTS = [
    ("～", "〜"),  # 全形波浪號 U+FF5E ↔ 波浪線 U+301C
    ("～", "~"),
    ("...", "…"),
    ("！！", "!!"),
    ("？", "?"),
    ("！", "!"),
    ("（", "("),
    ("）", ")"),
]


def restore_symbol_forms(original: str, new_text: str) -> str:
    for orig_form, ai_form in SYMBOL_VARIANTS:
        if ai_form in new_text and orig_form in original and ai_form not in original:
            new_text = new_text.replace(ai_form, orig_form)
    return new_text


def _check_text(new_text: str, expected_tokens) -> str:
    """回傳問題描述，沒問題回傳空字串。"""
    if not isinstance(new_text, str) or not new_text.strip():
        return "內容是空的"
    if "<" in new_text:
        return "出現了 HTML 標籤"
    got = tokens_in(new_text)
    if sorted(got) != sorted(expected_tokens):
        return "格式記號（粗體、連結等）不完整"
    if not _nesting_ok(new_text):
        return "格式記號順序錯亂"
    return ""


def validate_result(section, parsed: dict, mode: str):
    """檢查 AI 回傳的內容，只保留合格的部分。
    parsed: {"items": {id: text}, "runs": {run_id: [paragraph, ...]}, "note": str}
    回傳 (result, problems)：result 只包含「通過驗證而且真的有改」的部分；problems 是中文問題描述。
    """
    items = section["items"]
    result = {"items": {}, "runs": {}, "note": (parsed.get("note") or "").strip()}
    problems = []
    got_items = parsed.get("items") or {}
    got_runs = parsed.get("runs") or {}

    for u in section["units"]:
        if u["type"] == "run":
            paras = got_runs.get(u["id"])
            if paras is None:
                continue  # 沒回傳視為不用改（prompt 允許省略）
            if not isinstance(paras, list):
                problems.append(f"{u['id']}：回傳格式錯誤")
                continue
            originals = [items[pid]["text"] for pid in u["para_ids"]]
            original_joined = "\n".join(originals)
            paras = [
                restore_symbol_forms(original_joined, p.strip()) for p in paras if isinstance(p, str) and p.strip()
            ]
            if mode == "light":
                if len(paras) != len(originals):
                    problems.append(f"{u['id']}：輕修模式下段落數量被改變了")
                    continue
                for pid, new in zip(u["para_ids"], paras):
                    issue = _check_text(new, tokens_in(items[pid]["text"]))
                    if issue:
                        problems.append(f"{pid}：{issue}")
                    elif new != items[pid]["text"]:
                        result["items"][pid] = new
            else:
                if not paras:
                    problems.append(f"{u['id']}：改寫結果是空的")
                    continue
                expected = [t for o in originals for t in tokens_in(o)]
                joined_tokens = [t for p in paras for t in tokens_in(p)]
                if any("<" in p for p in paras):
                    problems.append(f"{u['id']}：出現了 HTML 標籤")
                elif sorted(joined_tokens) != sorted(expected):
                    problems.append(f"{u['id']}：格式記號（粗體、連結等）不完整")
                elif not all(_nesting_ok(p) for p in paras):
                    problems.append(f"{u['id']}：格式記號被拆到不同段落或順序錯亂")
                elif paras != originals:
                    result["runs"][u["id"]] = paras
            continue

        for item_id in u["block"]["items"]:
            if item_id not in got_items:
                continue  # 沒回傳視為不用改
            new = got_items[item_id]
            it = items[item_id]
            if isinstance(new, str):
                new = restore_symbol_forms(it["text"], new.strip())
            issue = _check_text(new, tokens_in(it["text"]))
            if issue:
                problems.append(f"{item_id}：{issue}")
            elif new != it["text"].strip():
                result["items"][item_id] = new
    return result, problems


def result_has_changes(result) -> bool:
    return bool(result and (result.get("items") or result.get("runs")))


# ── 組回 HTML ───────────────────────────────────────────────────


def _block_with_items(block, items, new_texts: dict, render_item=None):
    """把區塊裡有修改的項目替換掉。render_item(item, new_text) 回傳要放進去的 HTML。"""
    raw = block["raw"]
    changes = []
    for item_id in block["items"]:
        if item_id in new_texts:
            it = items[item_id]
            s, e = it["span"]
            changes.append((s, e, render_item(it, new_texts[item_id])))
    for s, e, repl in sorted(changes, reverse=True):
        raw = raw[:s] + repl + raw[e:]
    return raw


def _final_item_html(item, new_text):
    return detokenize(new_text, item["tokens"])


def _run_tokmap(section, unit):
    tokmap = {}
    for pid in unit["para_ids"]:
        tokmap.update(section["items"][pid]["tokens"])
    return tokmap


def rebuild_section(section, result) -> str:
    """組出這個段落套用修改後的 HTML。result 為 None 或沒有修改時，回傳原始字串。"""
    original = "".join(b["raw"] for b in section["blocks"])
    if not result_has_changes(result):
        return original
    items = section["items"]
    new_items = result.get("items") or {}
    new_runs = result.get("runs") or {}
    out = []
    for u in section["units"]:
        if u["type"] == "run":
            if u["id"] in new_runs:
                tokmap = _run_tokmap(section, u)
                out.append(
                    NEW_PARAGRAPH_SEPARATOR.join(
                        NEW_PARAGRAPH_TEMPLATE.format(inner=detokenize(p, tokmap)) for p in new_runs[u["id"]]
                    )
                )
            else:
                out.append("".join(_block_with_items(b, items, new_items, _final_item_html) for b in u["blocks"]))
        else:
            out.append(_block_with_items(u["block"], items, new_items, _final_item_html))
    return "".join(out)


def build_final_content(content: str, section_records: list) -> str:
    """整篇文章套用修改後的內容：status 是 accepted 的段落用修改版，其他維持原文。"""
    sections = parse_sections(content)
    out = []
    for sec in sections:
        rec = section_records[sec["index"]] if sec["index"] < len(section_records) else None
        if rec and rec.get("status") == "accepted" and rec.get("result"):
            out.append(rebuild_section(sec, rec["result"]))
        else:
            out.append("".join(b["raw"] for b in sec["blocks"]))
    return "".join(out)


# ── 審查畫面 ────────────────────────────────────────────────────


def _diff_html(orig_plain: str, new_plain: str) -> str:
    out = []
    for sp in diff_spans(orig_plain, new_plain):
        if sp["tag"] == "equal":
            out.append(html_lib.escape(sp["text"]))
        else:
            out.append(
                '<span class="tp-change">'
                + '<del class="tp-del">' + html_lib.escape(sp["original"]) + "</del>"
                + '<ins class="tp-ins">' + html_lib.escape(sp["corrected"]) + "</ins>"
                + "</span>"
            )
    return "".join(out)


def _review_item_html(item, new_text):
    """輕修模式的審查呈現：純文字 diff（格式記號拿掉，比較好讀）。"""
    # 只替換 <p>/<hN> 的內容或文字片段本身，外層標籤原樣保留
    return _diff_html(_plain_from_tokens(item["text"], item["tokens"]), _plain_from_tokens(new_text, item["tokens"]))


def count_light_changes(section, result) -> int:
    if not result:
        return 0
    total = 0
    for item_id, new in (result.get("items") or {}).items():
        it = section["items"].get(item_id)
        if it:
            total += count_changes(
                diff_spans(_plain_from_tokens(it["text"], it["tokens"]), _plain_from_tokens(new, it["tokens"]))
            )
    return total


def _light_body(section, result):
    items = section["items"]
    new_items = (result or {}).get("items") or {}
    new_runs = (result or {}).get("runs") or {}
    out = []
    for u in section["units"]:
        if u["type"] == "run" and u["id"] in new_runs:
            # 理論上輕修不會有 run 層級的修改；萬一有，就直接顯示組回後的結果
            tokmap = _run_tokmap(section, u)
            out.append("".join("<p>" + detokenize(p, tokmap) + "</p>" for p in new_runs[u["id"]]))
            continue
        blocks = u["blocks"] if u["type"] == "run" else [u["block"]]
        for b in blocks:
            out.append(_block_with_items(b, items, new_items, _review_item_html))
    return "".join(out)


# ── 段落重寫的醒目提示 ──
# 把群組裡的段落切成句子，用 SequenceMatcher 比對原文與改寫後的句子序列：
# 沒有對上的句子（改了字、被移動、合併或拆開）兩邊都標起來。
# <mark> 只包「記號之間的純文字」，不會包到標籤，所以句子跨過粗體、連結的邊界也不會讓 HTML 巢狀錯亂。

SENTENCE_RE = re.compile(r"[^。！？!?\n]*(?:[。！？!?]+[」』）)]*|\n|$)")


def split_sentences(text: str):
    """切句子；所有片段依序串起來等於原文字串（記號也在裡面）。"""
    return [s for s in SENTENCE_RE.findall(text) if s]


def _sentence_key(sentence: str) -> str:
    return ANY_BRACKET_TOKEN_RE.sub("", sentence).strip()


def changed_sentences(orig_paras, new_paras):
    """回傳 (原文每段要標記的句子 index 集合, 改寫後每段要標記的句子 index 集合)。"""
    def _flatten(paras):
        flat = []
        for pi, p in enumerate(paras):
            for si, s in enumerate(split_sentences(p)):
                key = _sentence_key(s)
                if key:  # 只有記號或空白的片段不參與比對
                    flat.append((pi, si, key))
        return flat

    a, b = _flatten(orig_paras), _flatten(new_paras)
    a_marks = [set() for _ in orig_paras]
    b_marks = [set() for _ in new_paras]
    sm = SequenceMatcher(None, [k for _, _, k in a], [k for _, _, k in b], autojunk=False)
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            continue
        for pi, si, _ in a[i1:i2]:
            a_marks[pi].add(si)
        for pi, si, _ in b[j1:j2]:
            b_marks[pi].add(si)
    return a_marks, b_marks


def _mark_pieces(text: str, tokmap: dict) -> str:
    """還原成 HTML，並把記號之間的文字用 <mark class="sec-hl"> 包起來。"""
    out = []
    pos = 0
    for m in ANY_BRACKET_TOKEN_RE.finditer(text):
        if m.start() > pos:
            out.append(_wrap_mark(text[pos : m.start()]))
        out.append(tokmap.get(m.group(0), m.group(0)))
        pos = m.end()
    if pos < len(text):
        out.append(_wrap_mark(text[pos:]))
    return "".join(out)


def _wrap_mark(s: str) -> str:
    return '<mark class="sec-hl">' + s + "</mark>" if s.strip() else s


def _render_paragraph_marked(text: str, tokmap: dict, marked: set) -> str:
    return "".join(
        _mark_pieces(s, tokmap) if si in marked else detokenize(s, tokmap)
        for si, s in enumerate(split_sentences(text))
    )


def rewrite_side_html(section, result, side: str) -> str:
    """段落重寫卡片的其中一欄（side="orig" 原文 / "new" 改寫後），受影響的句子加上醒目提示。"""
    items = section["items"]
    new_items = (result or {}).get("items") or {}
    new_runs = (result or {}).get("runs") or {}
    if side == "orig":
        render_item = lambda it, _new: _mark_pieces(it["text"], it["tokens"])  # noqa: E731
    else:
        render_item = lambda it, new: _mark_pieces(new, it["tokens"])  # noqa: E731
    out = []
    for u in section["units"]:
        if u["type"] == "run" and u["id"] in new_runs:
            tokmap = _run_tokmap(section, u)
            orig_paras = [items[pid]["text"] for pid in u["para_ids"]]
            a_marks, b_marks = changed_sentences(orig_paras, new_runs[u["id"]])
            paras, marks = (orig_paras, a_marks) if side == "orig" else (new_runs[u["id"]], b_marks)
            out.append("".join("<p>" + _render_paragraph_marked(p, tokmap, mk) + "</p>" for p, mk in zip(paras, marks)))
            continue
        blocks = u["blocks"] if u["type"] == "run" else [u["block"]]
        for b in blocks:
            out.append(_block_with_items(b, items, new_items, render_item))
    return "".join(out)


def section_original_html(section) -> str:
    return "".join(b["raw"] for b in section["blocks"])


def section_summary(section, rec):
    """單一段落的狀態：changed、change_count（輕修才有）、failed。"""
    result = rec.get("result") if rec else None
    changed = result_has_changes(result)
    return {
        "changed": changed,
        "change_count": count_light_changes(section, result) if changed else 0,
        "failed": bool(rec and (rec.get("error") or rec.get("problems"))),
    }


def render_section_card(section, rec) -> str:
    idx = section["index"]
    mode = (rec or {}).get("mode") or "light"
    status = (rec or {}).get("status") or "accepted"
    result = (rec or {}).get("result")
    corrected = bool(rec and rec.get("corrected"))
    info = section_summary(section, rec)
    needs_ai = section_needs_ai(section)

    if not needs_ai:
        stat = "沒有需要校對的文字"
    elif not corrected:
        stat = "尚未校對"
    elif info["changed"]:
        stat = f"{info['change_count']} 處修改" if mode == "light" else "已改寫"
    else:
        stat = "沒有修改"

    classes = ["sec-card", f"mode-{mode}"]
    if info["changed"]:
        classes.append("is-changed")
        classes.append("is-" + status)
    if info["failed"]:
        classes.append("is-failed")

    other_mode = "rewrite" if mode == "light" else "light"
    actions = []
    if info["changed"]:
        actions.append(
            '<button type="button" class="sec-btn sec-accept' + (" on" if status == "accepted" else "")
            + '" data-act="accept">✓ 採用</button>'
        )
        actions.append(
            '<button type="button" class="sec-btn sec-reject' + (" on" if status == "rejected" else "")
            + '" data-act="reject">✕ 維持原文</button>'
        )
    if needs_ai and corrected:
        actions.append('<button type="button" class="sec-btn" data-act="redo">✎ 重新調整</button>')
    if needs_ai:
        actions.append(
            '<button type="button" class="sec-btn" data-act="switch" data-mode="' + other_mode + '">改用'
            + MODE_LABEL[other_mode] + "重跑</button>"
        )

    head = (
        '<header class="sec-head">'
        + '<span class="sec-no">' + str(idx) + "</span>"
        + '<span class="sec-title">' + html_lib.escape(section["heading"]) + "</span>"
        + '<span class="sec-mode">' + MODE_LABEL[mode] + "</span>"
        + '<span class="sec-stat">' + html_lib.escape(stat) + "</span>"
        + '<div class="sec-actions">' + "".join(actions) + "</div>"
        + "</header>"
    )

    extra = ""
    note = (result or {}).get("note") if result else ""
    if note:
        extra += '<div class="sec-note">📝 ' + html_lib.escape(note) + "</div>"
    if rec and rec.get("error"):
        extra += '<div class="sec-warn">⚠️ AI 處理失敗，這段保留原文：' + html_lib.escape(rec["error"]) + "</div>"
    if rec and rec.get("problems"):
        extra += (
            '<div class="sec-warn">⚠️ 有 ' + str(len(rec["problems"]))
            + " 個部分沒通過格式檢查，已保留原文（可以按「重新調整」再試一次）："
            + html_lib.escape("；".join(rec["problems"])) + "</div>"
        )

    if mode == "rewrite" and info["changed"]:
        body = (
            '<div class="sec-compare">'
            + '<div class="sec-col sec-col-orig"><div class="sec-col-label">原文</div>'
            + rewrite_side_html(section, result, "orig") + "</div>"
            + '<div class="sec-col sec-col-new"><div class="sec-col-label">改寫後</div>'
            + rewrite_side_html(section, result, "new") + "</div>"
            + "</div>"
        )
    elif info["changed"]:
        body = _light_body(section, result)
    else:
        body = section_original_html(section)

    return (
        '<section class="' + " ".join(classes) + '" data-section="' + str(idx) + '">'
        + head + extra + '<div class="sec-body">' + body + "</div></section>"
    )


def render_review(content: str, section_records: list) -> str:
    sections = parse_sections(content)
    cards = []
    for sec in sections:
        rec = section_records[sec["index"]] if sec["index"] < len(section_records) else None
        cards.append(render_section_card(sec, rec))
    return "".join(cards)
