"""
app.py
用來校對／查核テス日文部落格文章的本機（local）網頁工具。

有兩種獨立的「種類」(kind)，各自有自己的 session 檔案、備份、審核畫面，不會互相覆蓋：
    - grammar：日文文法校對，引擎是 Gemini API（呼叫 ai_corrector.py）。
      以 H2 段落為單位（section_diff.py），每段可以選「微調」或「段落重寫」，整段採用或維持原文。
    - factcheck：事實查核／改寫，這支程式本身不呼叫任何 AI，只負責把文章內容匯出成
      檔案，實際的查證與改寫是在 Cowork 對話裡由 Claude 直接讀取匯出檔、上網查證後，
      把結果寫回 sessions/factcheck/{post_id}.json（跟這支程式共用同一套審核/套用/備份邏輯）。

使用方式請參考 README.md：
    python app.py
    瀏覽器開啟 http://localhost:5055
"""
import json
import os
from datetime import datetime
from pathlib import Path

from bs4 import BeautifulSoup
from dotenv import load_dotenv
from flask import Flask, jsonify, render_template, request
from google.genai import errors as genai_errors

import ai_corrector
import html_diff
import section_diff
import wp_db

BASE_DIR = Path(__file__).parent
load_dotenv(BASE_DIR.parent / ".env")

KINDS = {"grammar", "factcheck"}

SESS_DIR = BASE_DIR / "sessions"
GRAMMAR_SESS_DIR = SESS_DIR / "grammar"
FACTCHECK_SESS_DIR = SESS_DIR / "factcheck"
FACTCHECK_EXPORT_DIR = BASE_DIR / "factcheck_exports"
BACKUP_DIR = BASE_DIR / "backups"
GRAMMAR_SESS_DIR.mkdir(parents=True, exist_ok=True)
FACTCHECK_SESS_DIR.mkdir(parents=True, exist_ok=True)
FACTCHECK_EXPORT_DIR.mkdir(exist_ok=True)
BACKUP_DIR.mkdir(exist_ok=True)

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
MODEL = os.getenv("GEMINI_MODEL", "gemini-3.5-flash-lite")
# 重寫模式需要比較強的重組能力，可以在 .env 另外指定模型；沒設定就跟輕修用同一個。
REWRITE_MODEL = os.getenv("GEMINI_REWRITE_MODEL") or MODEL
# Gemini 免費方案是「每分鐘最多 N 次請求」。單次 /prepare 請求最多打這麼多次 API，
# 打滿就先把已完成的部分存檔、回傳「請稍後再按一次繼續」，避免一次請求打太多次撞 429。
GEMINI_RPM_LIMIT = int(os.getenv("GEMINI_RPM_LIMIT", "15"))


def _is_rate_limit_error(e) -> bool:
    return isinstance(e, genai_errors.ClientError) and getattr(e, "code", None) == 429


# 日文校對 session 的格式版本。2 = 以 H2 段落為單位（sections）；舊版（沒有這個欄位）是以文字節點為單位。
GRAMMAR_SCHEMA_VERSION = 2
LEGACY_GRAMMAR_MESSAGE = "這份草稿是舊版格式（逐處審查），新版改成以 H2 段落為單位，請重新按「開始 AI 校對」。"

_client = None


def get_client():
    global _client
    if _client is None:
        if not GEMINI_API_KEY:
            return None
        from google import genai

        _client = genai.Client(api_key=GEMINI_API_KEY)
    return _client


def _migrate_legacy_sessions():
    """舊版本把校對 session 直接存在 sessions/{id}.json，這裡搬進 sessions/grammar/。
    只搬「檔名是數字」的檔案，且目的地還不存在時才搬，避免不小心蓋掉或動到不相關的東西。
    """
    if not SESS_DIR.exists():
        return
    for p in SESS_DIR.glob("*.json"):
        if not p.is_file() or not p.stem.isdigit():
            continue
        target = GRAMMAR_SESS_DIR / p.name
        if not target.exists():
            p.rename(target)


_migrate_legacy_sessions()

app = Flask(__name__)


# ── Session（每篇文章、每種 kind 的校對/查核進度）存檔/讀取 ──────────

def sess_dir_for(kind: str) -> Path:
    return GRAMMAR_SESS_DIR if kind == "grammar" else FACTCHECK_SESS_DIR


def session_path(kind: str, post_id: int) -> Path:
    return sess_dir_for(kind) / f"{post_id}.json"


def load_session(kind: str, post_id: int):
    p = session_path(kind, post_id)
    if p.exists():
        return json.loads(p.read_text(encoding="utf-8"))
    return None


def save_session(kind: str, post_id: int, data: dict):
    data["kind"] = kind
    session_path(kind, post_id).write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def session_badge(kind: str, post_id: int):
    """給文章清單用的簡易狀態：None＝沒有草稿、"pending"＝審核中、"applied"＝已套用。"""
    session = load_session(kind, post_id)
    if not session:
        return None
    return "applied" if session.get("applied") else "pending"


def session_to_node_records(session: dict):
    """把 session["nodes"]（存檔用的 dict 陣列）轉成 html_diff.build_html 需要的
    格式（protected_original / protected_corrected / shortcode_map / statuses / sources / note）。
    sources、note 是事實查核專用的查證來源／筆記，文法校對的節點沒有這兩個欄位也沒關係。
    """
    records = []
    for n in session["nodes"]:
        if n is None:
            records.append(None)
            continue
        records.append(
            {
                "protected_original": n["protected_original"],
                "protected_corrected": n["protected_corrected"],
                "shortcode_map": n["shortcode_map"],
                "statuses": n["statuses"],
                "sources": n.get("sources"),
                "note": n.get("note"),
            }
        )
    return records


def is_v2_grammar(session: dict) -> bool:
    return bool(session) and session.get("schema_version") == GRAMMAR_SCHEMA_VERSION


def build_summary(session: dict):
    """組成畫面上方顯示的變更件數摘要。
    - 日文校對（v2）：以段落計算——有修改的段落數、採用／維持原文各幾段、有幾段處理失敗。
    - 事實查核（以及舊版日文校對）：統計所有節點的 statuses。
    兩種都會有 total_changes / accepted / rejected，草稿管理頁面共用。
    """
    if is_v2_grammar(session):
        sections = section_diff.parse_sections(session["raw_original_content"])
        recs = session.get("sections") or []
        changed = accepted = rejected = failed = 0
        for sec in sections:
            rec = recs[sec["index"]] if sec["index"] < len(recs) else None
            info = section_diff.section_summary(sec, rec)
            failed += 1 if info["failed"] else 0
            if not info["changed"]:
                continue
            changed += 1
            if rec.get("status") == "rejected":
                rejected += 1
            else:
                accepted += 1
        return {
            "unit": "section",
            "sections": len(sections),
            "total_changes": changed,
            "accepted": accepted,
            "rejected": rejected,
            "failed": failed,
        }

    total = accepted = rejected = 0
    for n in session.get("nodes") or []:
        if not n:
            continue
        for st in n["statuses"]:
            total += 1
            if st == "accepted":
                accepted += 1
            else:
                rejected += 1
    return {"unit": "change", "total_changes": total, "accepted": accepted, "rejected": rejected}


def default_title_check(title: str) -> dict:
    """標題 AI 檢查尚未執行時的預設值（先把原標題當作已選定）。"""
    return {"original": title, "grammar_ok": True, "note": "", "suggestions": [], "selected": title}


def staleness_info(session: dict):
    """比對資料庫目前內容跟 session 存的 baseline 是否一致，純比對不做任何修改。
    不同的話代表這段期間文章被改過（WP 後台手動編輯，或另一種 kind 已經套用過），
    只回傳警告文字讓畫面顯示，不會自動重新產生、也不會動到使用者已核准/拒絕的紀錄。
    已經套用過的 session 不需要再比（套用當下內容本來就會變，比較沒有意義）。
    """
    if session.get("applied"):
        return {"stale": False, "reason": ""}
    try:
        current = wp_db.get_post(session["post_id"])
    except Exception:
        return None  # 資料庫連不上，交給呼叫端的其他錯誤處理，這裡不擋
    if not current:
        return {"stale": True, "reason": "這篇文章在 WordPress 裡已經找不到了（可能已被刪除）。"}
    if current["content"] != session["raw_original_content"] or current["title"] != session["title"]:
        return {
            "stale": True,
            "reason": "這篇文章自從產生這份草稿後已經被修改過（可能是在 WP 後台手動編輯，"
            "或另一種校對／查核已經套用過），下面顯示的仍是產生當下的比對結果，建議重新產生。",
        }
    return {"stale": False, "reason": ""}


def build_review_response(kind: str, session: dict):
    """/prepare、/review 等多個 API 共用，組出前端審核畫面需要的資料
    （diff HTML + 摘要 + 標題建議 + 是否已過期）。
    """
    if kind == "grammar":
        review_html = section_diff.render_review(session["raw_original_content"], session.get("sections") or [])
    else:
        node_records = session_to_node_records(session)
        review_html = html_diff.build_html(session["raw_original_content"], node_records, mode="review", kind=kind)
    resp = {
        "post_id": session["post_id"],
        "kind": kind,
        "title": session["title"],
        "slug": session["slug"],
        "review_html": review_html,
        "summary": build_summary(session),
        "prepared_at": session.get("prepared_at"),
        "applied": session.get("applied", False),
    }
    # 標題 AI 檢查只有 grammar 有（呼叫 Gemini 產生建議）。事實查核不檢查標題，
    # 畫面上也不需要顯示這塊，所以 factcheck 完全不回傳 title_check。
    if kind == "grammar":
        resp["title_check"] = session.get("title_check") or default_title_check(session["title"])
        resp["mode"] = session.get("mode") or "light"
    stale = staleness_info(session)
    if stale:
        resp["stale"] = stale["stale"]
        resp["stale_reason"] = stale["reason"]
    return resp


# ── 路由（Routing） ────────────────────────────────────────────────

@app.route("/")
def index():
    """回傳首頁樣板。之後所有畫面操作都是 index.html 裡的 JS
    呼叫這支檔案的 /api/... 端點來完成。
    """
    return render_template("index.html", has_api_key=bool(GEMINI_API_KEY))


@app.route("/api/posts")
def api_posts():
    """左側側邊欄的文章列表。有搜尋字串 q 就過濾，
    並在每篇文章附上兩種 kind 各自的草稿狀態（grammar_session / factcheck_session）。
    """
    search = request.args.get("q", "").strip()
    try:
        posts = wp_db.list_posts(search)
    except Exception as e:
        return jsonify({"error": f"資料庫連線失敗：{e}"}), 500
    for p in posts:
        p["grammar_session"] = session_badge("grammar", p["id"])
        p["factcheck_session"] = session_badge("factcheck", p["id"])
    return jsonify(posts)


@app.route("/api/sessions")
def api_sessions():
    """「Session 管理」分頁用：把 grammar/factcheck 兩個資料夾裡所有現存的
    session 攤平列出來，方便一次看到全部累積的草稿並清除不需要的。
    """
    items = []
    for kind in KINDS:
        for p in sorted(sess_dir_for(kind).glob("*.json")):
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                continue
            note_count = sum(1 for n in (data.get("nodes") or []) if n and n.get("note"))
            # post_id 一定要用「檔名」而不是檔案內容裡存的 post_id 欄位——
            # 早期測試留下的幾份草稿（1.json、2.json...）內容裡的 post_id 是 0，
            # 跟檔名對不上，如果照內容的 post_id 組刪除網址會刪到不存在的檔案，
            # 畫面上看起來就像「按了刪除但沒反應」。檔名才是 session_path() 真正拿來
            # 找檔案的依據，這裡也要跟著用檔名，才能保證「畫面上顯示的這一列」跟
            # 「按刪除會刪到的檔案」是同一份。
            items.append(
                {
                    "post_id": p.stem,
                    "kind": kind,
                    "title": data.get("title") or f"（沒有標題 / 檔案 {p.name}）",
                    "prepared_at": data.get("prepared_at"),
                    "applied": data.get("applied", False),
                    "applied_at": data.get("applied_at"),
                    "summary": build_summary(data),
                    "note_count": note_count,
                }
            )
    items.sort(key=lambda x: x.get("prepared_at") or "", reverse=True)
    return jsonify(items)


@app.route("/api/sessions/<kind>/<int:post_id>", methods=["DELETE"])
def api_delete_session(post_id, kind):
    """「Session 管理」分頁的刪除鈕：只刪除本機的審核草稿檔案，
    不會動到 WordPress 資料庫裡的文章本身。
    """
    if kind not in KINDS:
        return jsonify({"error": f"不支援的種類：{kind}"}), 400
    p = session_path(kind, post_id)
    if p.exists():
        p.unlink()
    return jsonify({"ok": True})


@app.route("/api/sessions/delete", methods=["POST"])
def api_delete_sessions_batch():
    """「草稿管理」分頁的批次刪除：body 是 {"items": [{"kind": "grammar", "post_id": "6809"}, ...]}。
    一樣只刪本機的草稿檔案，不動 WordPress。post_id 必須是純數字（就是檔名），避免組出奇怪的路徑。
    """
    body = request.get_json(silent=True) or {}
    items = body.get("items")
    if not isinstance(items, list) or not items:
        return jsonify({"error": "沒有選取任何草稿"}), 400
    deleted = skipped = 0
    for it in items:
        it = it if isinstance(it, dict) else {}
        kind = it.get("kind")
        post_id = str(it.get("post_id") or "")
        if kind not in KINDS or not post_id.isdigit():
            skipped += 1
            continue
        p = sess_dir_for(kind) / f"{post_id}.json"
        if p.exists():
            p.unlink()
            deleted += 1
    return jsonify({"ok": True, "deleted": deleted, "skipped": skipped})


@app.route("/api/posts/<int:post_id>/<kind>/prepare", methods=["POST"])
def api_prepare(post_id, kind):
    """對應「開始 / 重新」按鈕。grammar 跟 factcheck 的行為完全不同：
        - grammar：跟以前一樣，從資料庫抓文章、丟給 Gemini 逐批校正，存成新 session。
        - factcheck：不呼叫任何 AI，只把目前的文章內容匯出成檔案，
          回傳訊息請你到 Cowork 對話請 Claude 查核，查完的結果由 Claude 直接寫進 session。
    """
    if kind not in KINDS:
        return jsonify({"error": f"不支援的種類：{kind}"}), 400

    try:
        post = wp_db.get_post(post_id)
    except Exception as e:
        return jsonify({"error": f"資料庫連線失敗：{e}"}), 500
    if not post:
        return jsonify({"error": "找不到這篇文章"}), 404

    if kind == "factcheck":
        return _prepare_factcheck(post_id, post)

    body = request.get_json(silent=True) or {}
    force = bool(body.get("force"))
    mode = body.get("mode") or "light"
    if mode not in section_diff.MODES:
        return jsonify({"error": f"不支援的校對模式：{mode}"}), 400

    existing = load_session("grammar", post_id)
    if existing and not is_v2_grammar(existing):
        existing = None  # 舊版格式的草稿不能接續，直接重新開始
    if existing and not existing.get("grammar_complete", True):
        # 上次的校對還沒跑完（通常是撞到 Gemini 免費方案的速率限制）。
        # 只要文章內容沒有在期間被人動過，就自動接續，不會浪費已經校對好的部分；
        # 忽略 force——沒有必要因為使用者又點了一次「開始／重新」就把還沒送出的進度整篇丟掉重打。
        stale = staleness_info(existing)
        if not (stale and stale["stale"]) and existing.get("mode", "light") == mode:
            return _prepare_grammar(post_id, post, mode, resume_session=existing)
        # 內容已變更（或換了模式），既有的未完成進度不適用，改成整篇重新開始
    elif existing and not force:
        return jsonify(build_review_response("grammar", existing))

    return _prepare_grammar(post_id, post, mode)


def _model_for(mode: str) -> str:
    return REWRITE_MODEL if mode == "rewrite" else MODEL


def _run_section(client, section, rec, budget, instruction=None):
    """把一個段落送給 AI 並驗證結果，寫回 rec。格式驗證沒過、而且還有額度時，自動重試 1 次。
    budget: {"left": 這次請求還能打幾次 API}，會被扣掉。
    撞到 429 會直接往外丟，讓呼叫端停下來存檔；其他錯誤記在 rec["error"]，這段保留原文。
    回傳 True 代表這段已經處理完（不論成功或失敗）；False 代表額度不夠、還沒處理。
    """
    mode = rec["mode"]
    payload = section_diff.ai_payload(section)
    previous = section_diff.result_to_ai_format(rec.get("result")) if instruction else None
    best = None
    last_error = None
    attempted = False
    for _attempt in range(2):
        if budget["left"] <= 0:
            break
        budget["left"] -= 1
        attempted = True
        try:
            parsed = ai_corrector.correct_section(
                client, _model_for(mode), payload, mode, instruction=instruction, previous=previous
            )
        except Exception as e:
            if _is_rate_limit_error(e):
                raise
            last_error = str(e)
            continue
        result, problems = section_diff.validate_result(section, parsed, mode)
        if best is None or len(problems) < len(best[1]):
            best = (result, problems)
        if not problems:
            break
    if not attempted:
        return False
    if best is None:
        rec.update({"result": None, "problems": [], "error": last_error or "未知的錯誤"})
    else:
        rec.update({"result": best[0], "problems": best[1], "error": None})
    rec["status"] = "accepted"
    rec["corrected"] = True
    return True


def _new_section_record(section, mode):
    return {
        "index": section["index"],
        "heading": section["heading"],
        "mode": mode,
        "status": "accepted",
        # 沒有任何日文文字的段落（例如只有圖片）不用送 AI，直接當作處理完
        "corrected": not section_diff.section_needs_ai(section),
        "result": None,
        "problems": [],
        "error": None,
    }


def _prepare_grammar(post_id, post, mode, resume_session=None):
    """以 H2 段落為單位校對一篇文章，一段打一次 API。單次呼叫最多打 GEMINI_RPM_LIMIT 次
    （避免撞到免費方案「每分鐘限 N 次請求」的上限），還沒處理完的段落標記 corrected=False 並存檔，
    下次呼叫時（resume_session 有帶值）從沒完成的地方繼續，不會重打已經成功的部分。
    """
    client = get_client()
    if client is None:
        return jsonify({"error": "GEMINI_API_KEY 未設定。請在專案根目錄的 .env 中新增 GEMINI_API_KEY。"}), 400

    if resume_session is not None:
        session = resume_session
        raw_content = session["raw_original_content"]
        sections = section_diff.parse_sections(raw_content)
    else:
        raw_content = post["content"]
        sections = section_diff.parse_sections(raw_content)
        session = {
            "schema_version": GRAMMAR_SCHEMA_VERSION,
            "post_id": post_id,
            "title": post["title"],
            "slug": post["slug"],
            "raw_original_content": raw_content,
            "mode": mode,
            "sections": [_new_section_record(sec, mode) for sec in sections],
            "title_check": None,
            "prepared_at": datetime.now().isoformat(timespec="seconds"),
            "applied": False,
            "grammar_complete": False,
        }
    records = session["sections"]

    errors = []
    budget = {"left": GEMINI_RPM_LIMIT}
    rate_limited = False
    for sec in sections:
        rec = records[sec["index"]]
        if rec.get("corrected"):
            continue
        try:
            done = _run_section(client, sec, rec, budget)
        except Exception as e:
            if _is_rate_limit_error(e):
                errors.append("Gemini 免費方案已達每分鐘請求上限，還沒處理的段落已保留原文，下次會自動繼續處理。")
                rate_limited = True
                save_session("grammar", post_id, session)
                break
            raise
        save_session("grammar", post_id, session)
        if not done:
            break

    remaining = [r for r in records if not r.get("corrected")]
    if remaining:
        session["grammar_complete"] = False
        save_session("grammar", post_id, session)
        resp = build_review_response("grammar", session)
        progress = (
            f"已完成 {len(records) - len(remaining)}/{len(records)} 段校正，"
            f"已超過 Gemini 免費額度，請稍等約 1 分鐘後"
            f"再按一次「開始／重新」繼續校對剩下的 {len(remaining)} 段（已校對好的部分不會遺失）。"
        )
        resp["partial"] = True
        resp["warnings"] = [progress] + errors
        return jsonify(resp)

    # 內文全部校對完成，接著處理標題建議（如果還沒做過，且這次還有餘裕）
    if session.get("title_check") is None:
        if budget["left"] <= 0 or rate_limited:
            session["grammar_complete"] = False
            save_session("grammar", post_id, session)
            resp = build_review_response("grammar", session)
            resp["partial"] = True
            resp["warnings"] = ["內文已完成校對，只差最後的標題建議，請再按一次「開始／重新」完成最後一步。"]
            return jsonify(resp)

        plain_text = BeautifulSoup(raw_content, "html.parser").get_text("\n", strip=True)
        title_check = default_title_check(session["title"])
        try:
            result = ai_corrector.suggest_title(client, MODEL, session["title"], plain_text)
            title_check.update(
                {
                    "grammar_ok": result["grammar_ok"],
                    "note": result["note"],
                    "suggestions": result["suggestions"],
                }
            )
        except Exception as e:
            if _is_rate_limit_error(e):
                errors.append("Gemini 免費方案已達每分鐘請求上限，標題建議請稍後再按一次「開始／重新」重試。")
            else:
                errors.append(f"標題建議失敗：{e}")
        session["title_check"] = title_check

    session["grammar_complete"] = True
    save_session("grammar", post_id, session)

    resp = build_review_response("grammar", session)
    failed = [r for r in records if r.get("error") or r.get("problems")]
    if failed:
        errors.append(
            f"有 {len(failed)} 段 AI 處理失敗或部分沒通過格式檢查，那些部分已保留原文，可以在該段按「重新調整」再試一次。"
        )
    if errors:
        resp["warnings"] = errors
    return jsonify(resp)


def _prepare_factcheck(post_id, post):
    raw_content = post["content"]
    soup, dom_nodes = html_diff.extract_nodes(raw_content)

    nodes_export = []
    for idx, dom_node in enumerate(dom_nodes):
        original_text = str(dom_node)
        protected, shortcode_map = html_diff.protect_shortcodes(original_text)
        nodes_export.append({"index": idx, "text": protected, "shortcode_map": shortcode_map})

    plain_text = BeautifulSoup(raw_content, "html.parser").get_text("\n", strip=True)

    export = {
        "post_id": post_id,
        "title": post["title"],
        "slug": post["slug"],
        "exported_at": datetime.now().isoformat(timespec="seconds"),
        "raw_original_content": raw_content,
        "plain_text": plain_text,
        "nodes": nodes_export,
    }
    export_path = FACTCHECK_EXPORT_DIR / f"{post_id}.json"
    export_path.write_text(json.dumps(export, ensure_ascii=False, indent=2), encoding="utf-8")

    existing = load_session("factcheck", post_id)
    resp = {
        "post_id": post_id,
        "title": post["title"],
        "exported": True,
        "export_file": str(export_path),
        "message": f"已把文章內容匯出到 factcheck_exports/{post_id}.json，還沒有查核結果。",
        "has_existing_session": bool(existing),
        "existing_prepared_at": existing.get("prepared_at") if existing else None,
    }
    return jsonify(resp)


@app.route("/api/posts/<int:post_id>/<kind>/review")
def api_review(post_id, kind):
    """從已存在的 session 讀出目前的審核狀況（diff HTML 等）並回傳。
    不會呼叫 AI，純粹是重新顯示既有 session（例如重新選到同一篇文章時使用）。
    """
    if kind not in KINDS:
        return jsonify({"error": f"不支援的種類：{kind}"}), 400
    session = load_session(kind, post_id)
    if not session:
        if kind == "grammar":
            return jsonify({"error": "尚未產生校對建議，請先按「開始 AI 校對」"}), 404
        return jsonify({"error": "這篇文章還沒有查核結果，請先按「開始查核」匯出內容，再到 Cowork 對話請 Claude 查核。"}), 404
    if kind == "grammar" and not is_v2_grammar(session):
        return jsonify({"error": LEGACY_GRAMMAR_MESSAGE, "legacy": True}), 410
    return jsonify(build_review_response(kind, session))


def _load_editable_grammar_session(post_id, kind):
    """段落操作 API 共用的前置檢查。成功回傳 (session, None)，失敗回傳 (None, 錯誤回應)。"""
    if kind not in KINDS:
        return None, (jsonify({"error": f"不支援的種類：{kind}"}), 400)
    if kind != "grammar":
        return None, (jsonify({"error": "事實查核是筆記功能，沒有採用/維持原文或重新調整，直接看筆記自己判斷就好。"}), 400)
    session = load_session("grammar", post_id)
    if not session:
        return None, (jsonify({"error": "找不到校對進度"}), 404)
    if not is_v2_grammar(session):
        return None, (jsonify({"error": LEGACY_GRAMMAR_MESSAGE}), 410)
    if session.get("applied"):
        return None, (
            jsonify({"error": "這份草稿已經套用覆蓋原文了，內容已經寫進 WordPress，不能再修改。如需再調整，請重新產生一份新的校對。"}),
            400,
        )
    return session, None


def _section_from_body(session, body):
    sections = section_diff.parse_sections(session["raw_original_content"])
    try:
        idx = int(body.get("section_index"))
        if idx < 0:
            raise IndexError
        return sections[idx], session["sections"][idx]
    except (TypeError, ValueError, IndexError):
        return None, None


@app.route("/api/posts/<int:post_id>/<kind>/section/status", methods=["POST"])
def api_section_status(post_id, kind):
    """把某一個 H2 段落切換成 accepted（採用）/ rejected（維持原文）。"""
    session, err = _load_editable_grammar_session(post_id, kind)
    if err:
        return err
    body = request.get_json(force=True)
    status = body.get("status")
    if status not in ("accepted", "rejected"):
        return jsonify({"error": "status 必須是 accepted 或 rejected"}), 400
    section, rec = _section_from_body(session, body)
    if section is None:
        return jsonify({"error": "section_index 超出範圍"}), 400

    rec["status"] = status
    save_session(kind, post_id, session)
    return jsonify(
        {"ok": True, "card_html": section_diff.render_section_card(section, rec), "summary": build_summary(session)}
    )


@app.route("/api/posts/<int:post_id>/<kind>/redo", methods=["POST"])
def api_redo(post_id, kind):
    """重新處理一個 H2 段落。兩種用法：
        - 「✎ 重新調整」：帶 instruction（使用者意見），沿用這段目前的模式，並把上一次的結果給 AI 參考。
        - 「改用微調／段落重寫重跑」：帶 mode、不帶 instruction，用新的模式從原文重新處理這段。
    只有 grammar 支援。factcheck 沒有 in-app 的 AI 可以即時重跑。
    """
    session, err = _load_editable_grammar_session(post_id, kind)
    if err:
        return err
    client = get_client()
    if client is None:
        return jsonify({"error": "GEMINI_API_KEY 未設定"}), 400

    body = request.get_json(force=True)
    section, rec = _section_from_body(session, body)
    if section is None:
        return jsonify({"error": "section_index 超出範圍"}), 400
    if not section_diff.section_needs_ai(section):
        return jsonify({"error": "這段沒有需要校對的日文文字"}), 400

    instruction = (body.get("instruction") or "").strip()
    mode = body.get("mode") or rec.get("mode") or "light"
    if mode not in section_diff.MODES:
        return jsonify({"error": f"不支援的校對模式：{mode}"}), 400
    if mode != rec.get("mode"):
        rec["result"] = None  # 換模式就從原文重新來，不參考另一種模式的結果
    rec["mode"] = mode

    try:
        _run_section(client, section, rec, {"left": 2}, instruction=instruction or None)
    except Exception as e:
        if _is_rate_limit_error(e):
            return jsonify({"error": "Gemini 免費方案已達每分鐘請求上限，請稍等約 1 分鐘再試。"}), 429
        return jsonify({"error": f"呼叫 AI 失敗：{e}"}), 500
    save_session("grammar", post_id, session)

    return jsonify(
        {"ok": True, "card_html": section_diff.render_section_card(section, rec), "summary": build_summary(session)}
    )


@app.route("/api/posts/<int:post_id>/<kind>/title/select", methods=["POST"])
def api_title_select(post_id, kind):
    """使用者從 AI 建議的標題清單中選定一個（或自行輸入），
    只更新 session 裡的 title_check.selected，還沒真的寫回資料庫。
    """
    if kind not in KINDS:
        return jsonify({"error": f"不支援的種類：{kind}"}), 400
    if kind != "grammar":
        return jsonify({"error": "事實查核不檢查標題，沒有這個功能。"}), 400
    session = load_session(kind, post_id)
    if not session:
        return jsonify({"error": "找不到審核進度"}), 404

    body = request.get_json(force=True)
    chosen = (body.get("title") or "").strip()
    if not chosen:
        return jsonify({"error": "標題不可為空"}), 400

    title_check = session.get("title_check") or default_title_check(session["title"])
    title_check["selected"] = chosen
    session["title_check"] = title_check
    save_session(kind, post_id, session)
    return jsonify({"ok": True, "selected": chosen})


@app.route("/api/posts/<int:post_id>/<kind>/apply", methods=["POST"])
def api_apply(post_id, kind):
    """「套用並覆蓋原文」按鈕：把 session 中所有 accepted 的變更套用到內文，
    正式寫回 WordPress 資料庫。寫入前會先備份原文，且會檢查文章/標題
    是否在校對期間被人在 WP 後台手動改過。

    只有 grammar 支援。事實查核是筆記功能，不寫回 WordPress——查完的結果只給人看，
    要不要動筆、怎麼改寫由テスさん自己決定並手動編輯，畫面上也沒有這顆按鈕。
    """
    if kind not in KINDS:
        return jsonify({"error": f"不支援的種類：{kind}"}), 400
    if kind != "grammar":
        return jsonify({"error": "事實查核沒有套用功能，這是筆記功能，請自己看筆記手動改寫文章。"}), 400
    session = load_session(kind, post_id)
    if not session:
        return jsonify({"error": "找不到審核進度"}), 404
    if not is_v2_grammar(session):
        return jsonify({"error": LEGACY_GRAMMAR_MESSAGE}), 410
    if session.get("applied"):
        return jsonify({"error": "這份草稿已經套用過了。"}), 400
    if not session.get("grammar_complete", True):
        return jsonify(
            {"error": "校對尚未完成，還有部分內容還沒送到 AI 校對，請先按「開始／重新」繼續完成校對，才能套用。"}
        ), 400

    try:
        current_post = wp_db.get_post(post_id)
    except Exception as e:
        return jsonify({"error": f"資料庫連線失敗：{e}"}), 500
    if not current_post:
        return jsonify({"error": "找不到這篇文章（可能已被刪除）"}), 404

    # 若目前資料庫內容跟開始校對/查核時不同，代表有人在 WP 後台改過文章，
    # 或另一種 kind 已經先套用過，為避免互相覆蓋，拒絕套用
    if current_post["content"] != session["raw_original_content"]:
        return jsonify(
            {
                "error": "偵測到這篇文章的內容已經被修改過（可能是 WP 後台手動編輯，"
                "或另一種校對／查核已經先套用過），為避免覆蓋掉新的修改，請重新產生這份草稿後再套用。"
            }
        ), 409

    title_check = session.get("title_check") or default_title_check(session["title"])
    final_title = title_check.get("selected") or session["title"]

    # 若標題有變更，且資料庫內標題也跟開始校對時不同，同樣視為被人動過，拒絕覆蓋
    if final_title != session["title"] and current_post["title"] != session["title"]:
        return jsonify(
            {
                "error": "偵測到這篇文章的標題已經被修改過，為避免覆蓋掉新的修改，"
                "請重新產生這份草稿後再套用。"
            }
        ), 409

    final_content = section_diff.build_final_content(session["raw_original_content"], session["sections"])

    # 覆蓋前先備份原文（標題+內文），檔名帶上 kind 以區分是哪種校對/查核的備份
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup_file = BACKUP_DIR / f"{post_id}-{session.get('slug') or 'post'}-{kind}-{timestamp}.json"
    backup_file.write_text(
        json.dumps(
            {"title": session["title"], "content": session["raw_original_content"]},
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    try:
        wp_db.update_post(post_id, content=final_content, title=final_title if final_title != session["title"] else None)
    except Exception as e:
        return jsonify({"error": f"寫入資料庫失敗：{e}"}), 500

    session["applied"] = True
    session["applied_at"] = datetime.now().isoformat(timespec="seconds")
    session["backup_file"] = str(backup_file)
    save_session(kind, post_id, session)

    return jsonify({"ok": True, "backup_file": str(backup_file), "final_title": final_title})


if __name__ == "__main__":
    if not GEMINI_API_KEY:
        print("⚠️  GEMINI_API_KEY 未設定，日文校對功能無法使用。請在專案根目錄 .env 加入這一行：")
        print("    GEMINI_API_KEY=AIzaSy........")
        print("    （事實查核不需要這組 Key，靠 Cowork 對話裡的 Claude 直接處理）")
    app.run(host="127.0.0.1", port=5055, debug=True)
