"""
ai_corrector.py
呼叫 Gemini API，依照「テスさん的文體風格指南」以 H2 段落為單位校正日文（輕修 / 重寫）。
文章標題的文法檢查與修改建議也在這裡處理。
段落要怎麼切、送什麼內容、回傳怎麼驗證，見 section_diff.py。
"""
import json
import os
import re
import time

from google.genai import errors as genai_errors
from google.genai import types

from prompts import build_section_system_prompt, build_title_system_prompt

# app.py 那邊已經會把單次請求要打的 API 次數限制在這個數字以內，
# 這裡的節流只是防呆（例如兩個請求前後腳很接近時），正常情況下很少真的需要 sleep。
GEMINI_RPM_LIMIT = int(os.getenv("GEMINI_RPM_LIMIT", "15"))
GEMINI_MAX_RETRIES = int(os.getenv("GEMINI_MAX_RETRIES", "3"))
THROTTLE_WINDOW_SECONDS = 60
DEFAULT_RETRY_DELAY_SECONDS = 15.0
RETRY_DELAY_BUFFER_SECONDS = 2

# 輸出上限，依「這次回應內容量」抓：
# - 段落校正要回傳整個 H2 段落修正後的全文（目前所有文章裡最長的段落約 3,200 字），額度最大；
#   回應被截斷時 JSON 會解析失敗，這段會標示「AI 處理失敗、保留原文」，不會套用半截的內容。
# - 標題檢查只回一個固定格式的小型 JSON。
SECTION_MAX_OUTPUT_TOKENS = 12000
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


def _parse_json(raw: str) -> dict:
    raw = (raw or "").strip()
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        m = re.search(r"\{.*\}", raw, re.S)
        if not m:
            raise ValueError("AI 回傳的不是 JSON（可能被截斷了）")
        try:
            return json.loads(m.group(0))
        except json.JSONDecodeError as e:
            raise ValueError(f"AI 回傳的 JSON 格式錯誤（可能被截斷了）：{e}") from e


def correct_section(client, model: str, payload: list, mode: str, instruction: str = None, previous: dict = None) -> dict:
    """校正一個 H2 段落。
    payload: section_diff.ai_payload() 的結果。
    mode: "light"（輕修）或 "rewrite"（重寫）。
    instruction / previous: 「重新調整」時帶入使用者意見與上一次的結果（AI 輸出格式）。
    回傳值: {"items": {id: text}, "runs": {group_id: [paragraph, ...]}, "note": str}
    格式錯誤時丟出 ValueError。
    """
    system = build_section_system_prompt(mode)
    body = {"section": payload}
    if instruction:
        system += (
            "\n\n# 追加指示（今回のやり直し専用）\n"
            "ユーザーは前回の結果に納得していません。`previous_result` が前回の結果です。"
            "以下のユーザーの要望を最優先で反映して、このセクションをもう一度処理してください。"
            "要望が「直さないでほしい」という内容であれば、原文により近い形に戻してください。\n"
            f"ユーザーの要望: {instruction}\n"
        )
        if previous:
            body["previous_result"] = previous

    resp = _generate_with_retry(
        client,
        model=model,
        contents=json.dumps(body, ensure_ascii=False, indent=1),
        config=types.GenerateContentConfig(
            system_instruction=system,
            response_mime_type="application/json",
            max_output_tokens=SECTION_MAX_OUTPUT_TOKENS,
        ),
    )
    data = _parse_json(_response_text(resp))
    if not isinstance(data, dict):
        raise ValueError("AI 回傳的 JSON 格式不對")

    items = {}
    for it in data.get("items") or []:
        if isinstance(it, dict) and isinstance(it.get("id"), str) and isinstance(it.get("text"), str):
            items[it["id"]] = it["text"]
    runs = {}
    for g in data.get("groups") or []:
        if isinstance(g, dict) and isinstance(g.get("id"), str) and isinstance(g.get("paragraphs"), list):
            runs[g["id"]] = g["paragraphs"]
    note = data.get("note") if isinstance(data.get("note"), str) else ""
    return {"items": items, "runs": runs, "note": note}


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
    try:
        data = _parse_json(_response_text(resp))
    except ValueError:
        data = {}

    suggestions = data.get("suggestions") or []
    if not isinstance(suggestions, list):
        suggestions = []
    suggestions = [s for s in suggestions if isinstance(s, str) and s.strip()][:TITLE_SUGGESTION_LIMIT]

    return {
        "grammar_ok": bool(data.get("grammar_ok", True)),
        "note": data.get("note") or "",
        "suggestions": suggestions,
    }
