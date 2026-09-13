"""
ai_corrector.py
呼叫 Gemini API，依照「テスさん的文體風格指南」校正日文文字節點。
文章標題的文法檢查與修改建議也在這裡處理。
"""
import json
import os
import re
import time

from google.genai import errors as genai_errors
from google.genai import types

from prompts import build_system_prompt, build_title_system_prompt

NODE_OPEN = "⟦N{i}⟧"   # ⟦N0⟧
NODE_CLOSE = "⟦/N{i}⟧"  # ⟦/N0⟧

# app.py 那邊已經會把單次請求要打的 API 次數限制在這個數字以內，
# 這裡的節流只是防呆（例如兩個請求前後腳很接近時），正常情況下很少真的需要 sleep。
GEMINI_RPM_LIMIT = int(os.getenv("GEMINI_RPM_LIMIT", "15"))
GEMINI_MAX_RETRIES = int(os.getenv("GEMINI_MAX_RETRIES", "3"))
THROTTLE_WINDOW_SECONDS = 60
DEFAULT_RETRY_DELAY_SECONDS = 15.0
RETRY_DELAY_BUFFER_SECONDS = 2

# 三種呼叫的輸出上限，依「這次回應內容量」抓：
# - 批次校正要 echo 回整批節點修正後的全文，額度最大；
# - 單節點 redo 只 echo 回一個節點；
# - 標題檢查只回一個固定格式的小型 JSON。
BATCH_MAX_OUTPUT_TOKENS = 8000
SINGLE_MAX_OUTPUT_TOKENS = 4000
TITLE_MAX_OUTPUT_TOKENS = 1500

TITLE_ARTICLE_EXCERPT_CHARS = 6000  # 本文太長時，用來判斷標題的開頭字數
TITLE_SUGGESTION_LIMIT = 3

_recent_call_times = []


def _throttle():
    """確保過去 THROTTLE_WINDOW_SECONDS 秒內的呼叫次數不超過 GEMINI_RPM_LIMIT，需要的話就等待。"""
    now = time.monotonic()
    window_start = now - THROTTLE_WINDOW_SECONDS
    while _recent_call_times and _recent_call_times[0] < window_start:
        _recent_call_times.pop(0)
    if len(_recent_call_times) >= GEMINI_RPM_LIMIT:
        wait = _recent_call_times[0] + THROTTLE_WINDOW_SECONDS - now
        if wait > 0:
            time.sleep(wait)
    _recent_call_times.append(time.monotonic())


def _retry_delay_seconds(err: genai_errors.APIError) -> float:
    """從 429 錯誤的 details 裡找 RetryInfo，解析 Gemini 建議的等待秒數。找不到就用預設值。"""
    try:
        details = (err.details or {}).get("error", {}).get("details", [])
        for d in details:
            if str(d.get("@type", "")).endswith("RetryInfo"):
                raw = d.get("retryDelay", "")
                return float(str(raw).rstrip("s")) + RETRY_DELAY_BUFFER_SECONDS
    except (AttributeError, TypeError, ValueError):
        pass
    return DEFAULT_RETRY_DELAY_SECONDS


def _generate_with_retry(client, *, model, contents, config):
    """generate_content 的共用包裝：先節流，撞到 429 就照建議秒數等待後重試。"""
    last_err = None
    for attempt in range(GEMINI_MAX_RETRIES + 1):
        _throttle()
        try:
            return client.models.generate_content(model=model, contents=contents, config=config)
        except genai_errors.ClientError as e:
            if e.code != 429 or attempt == GEMINI_MAX_RETRIES:
                raise
            last_err = e
            time.sleep(_retry_delay_seconds(e))
    raise last_err


def _parse_batch_response(raw: str, indices) -> dict:
    results = {}
    for idx in indices:
        open_tag = re.escape(NODE_OPEN.format(i=idx))
        close_tag = re.escape(NODE_CLOSE.format(i=idx))
        m = re.search(open_tag + r"\n?(.*?)\n?" + close_tag, raw, re.S)
        if m:
            results[idx] = m.group(1)
    return results


def _response_text(resp) -> str:
    text = getattr(resp, "text", None)
    if text:
        return text
    # 備援：從 candidates 手動組出文字
    out = []
    for cand in getattr(resp, "candidates", []) or []:
        content = getattr(cand, "content", None)
        for part in getattr(content, "parts", []) or []:
            if getattr(part, "text", None):
                out.append(part.text)
    return "".join(out)


def correct_nodes_batch(client, model: str, nodes_texts):
    """nodes_texts: [(node_index, protected_text), ...]，一次 API 呼叫就把整批校正完。
    回傳值: {node_index: corrected_protected_text}
    """
    if not nodes_texts:
        return {}

    parts = [
        f"{NODE_OPEN.format(i=idx)}\n{text}\n{NODE_CLOSE.format(i=idx)}"
        for idx, text in nodes_texts
    ]
    user_content = "\n\n".join(parts)

    resp = _generate_with_retry(
        client,
        model=model,
        contents=user_content,
        config=types.GenerateContentConfig(
            system_instruction=build_system_prompt(),
            max_output_tokens=BATCH_MAX_OUTPUT_TOKENS,
        ),
    )
    raw = _response_text(resp)
    indices = [idx for idx, _ in nodes_texts]
    parsed = _parse_batch_response(raw, indices)

    # 解析失敗的區塊視為「沒有變更」，採用原文（安全優先）
    return {idx: parsed.get(idx, text) for idx, text in nodes_texts}


def correct_node_single(client, model: str, protected_text: str, instruction: str, previous_correction: str = None) -> str:
    """redo（只重做一個區塊），套用使用者的補充指示。"""
    system = build_system_prompt() + (
        "\n\n# 追加指示（今回のやり直し専用）\n"
        "ユーザーは前回の修正結果に納得していません。以下のユーザーの要望を最優先で反映して、"
        "もう一度このブロックだけを校正してください。要望が「直さないでほしい」という内容であれば、"
        "原文により近い形に戻してください。\n"
        f"ユーザーの要望: {instruction}\n"
    )
    prev = previous_correction or protected_text
    user_content = (
        f"{NODE_OPEN.format(i=0)}\n"
        f"[原文]\n{protected_text}\n\n[前回の修正結果]\n{prev}\n"
        f"{NODE_CLOSE.format(i=0)}\n\n"
        "上記の[原文]について、ユーザーの要望を反映した新しい修正結果だけを、"
        f"{NODE_OPEN.format(i=0)} と {NODE_CLOSE.format(i=0)} で囲んで返してください。"
    )
    resp = _generate_with_retry(
        client,
        model=model,
        contents=user_content,
        config=types.GenerateContentConfig(
            system_instruction=system,
            max_output_tokens=SINGLE_MAX_OUTPUT_TOKENS,
        ),
    )
    raw = _response_text(resp)
    parsed = _parse_batch_response(raw, [0])
    return parsed.get(0, protected_text).strip("\n")


def suggest_title(client, model: str, title: str, article_text: str) -> dict:
    """文章標題的文法檢查與改善建議。
    回傳值: {"grammar_ok": bool, "note": str, "suggestions": [str, ...]}
    """
    article_excerpt = article_text[:TITLE_ARTICLE_EXCERPT_CHARS]
    user_content = (
        f"現在のタイトル: {title}\n\n"
        f"本文:\n{article_excerpt}"
    )
    resp = _generate_with_retry(
        client,
        model=model,
        contents=user_content,
        config=types.GenerateContentConfig(
            system_instruction=build_title_system_prompt(),
            response_mime_type="application/json",
            max_output_tokens=TITLE_MAX_OUTPUT_TOKENS,
        ),
    )
    raw = _response_text(resp).strip()
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        m = re.search(r"\{.*\}", raw, re.S)
        data = json.loads(m.group(0)) if m else {}

    suggestions = data.get("suggestions") or []
    if not isinstance(suggestions, list):
        suggestions = []
    suggestions = [s for s in suggestions if isinstance(s, str) and s.strip()][:TITLE_SUGGESTION_LIMIT]

    return {
        "grammar_ok": bool(data.get("grammar_ok", True)),
        "note": data.get("note") or "",
        "suggestions": suggestions,
    }
