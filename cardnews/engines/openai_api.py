"""OpenAI Responses API 실행 엔진 (기본 엔진).

프롬프트·결과 형식(JSON 스키마)·결과 검사는 Claude 엔진(claude_cli.ClaudeCliEngine)과 같고,
호출만 OpenAI Responses API(`POST /v1/responses`)로 한다.
- 웹 조사: 내장 `web_search` 도구. 검색어와 연 페이지는 스트리밍 이벤트에서 받아 진행 내역에 남긴다.
- 결과 모양: `text.format`의 json_schema(strict). 스키마를 strict 규칙에 맞게 바꿔 보낸다.
- 질문 뒤 이어가기: 응답 ID를 세션 ID로 저장하고 `previous_response_id`로 같은 대화를 이어 간다.
- 키는 환경 변수 OPENAI_API_KEY에서만 읽고, 화면·로그·파일에 남기지 않는다.
"""
from __future__ import annotations

import copy
import json
import os
import queue
import threading
import time
import urllib.error
import urllib.request
from typing import Any

from . import Cancelled, Emit, EngineError
from .claude_cli import TIMEOUT_SECONDS, ClaudeCliEngine


class OpenAIEngine(ClaudeCliEngine):
    name = "openai"
    is_mock = False
    URL = "https://api.openai.com/v1/responses"

    def __init__(self, model: str = "gpt-5.4-mini", effort: str = "medium"):
        super().__init__(model=model, effort=effort)

    def check(self) -> str | None:
        if not os.environ.get("OPENAI_API_KEY"):
            return "OpenAI API 키(환경 변수 OPENAI_API_KEY)가 없습니다."
        return None

    def _call(
        self, prompt: str, schema: dict[str, Any], session_id: str | None, work_dir: str, emit: Emit,
        cancel: threading.Event, tools: str,
    ) -> dict[str, Any]:
        try:
            return self._request(prompt, schema, session_id, emit, cancel, tools)
        except EngineError as e:
            if e.code != "session_missing" or not session_id:
                raise
            # 저장된 앞 응답이 없으면(보관 기간이 지났거나 다른 키) 프롬프트에 담긴 내용으로 새로 시작한다
            emit("이전 대화를 찾지 못해, 저장된 선택과 답변으로 새로 시작합니다.")
            return self._request(prompt, schema, None, emit, cancel, tools)

    def _request(
        self, prompt: str, schema: dict[str, Any], session_id: str | None, emit: Emit,
        cancel: threading.Event, tools: str,
    ) -> dict[str, Any]:
        key = os.environ.get("OPENAI_API_KEY")
        if not key:
            raise EngineError("auth_required", "OpenAI API 키(환경 변수 OPENAI_API_KEY)가 없습니다.", "키를 환경 변수에 넣고 서버를 다시 켜세요.")
        body: dict[str, Any] = {
            "model": self.model,
            "input": prompt,
            "stream": True,
            "store": True,
            "reasoning": {"effort": self.effort},
            "text": {"format": {"type": "json_schema", "name": "result", "schema": _strict(schema), "strict": True}},
        }
        if tools:
            body["tools"] = [{"type": "web_search"}]
            body["include"] = ["web_search_call.action.sources"]
        if session_id:
            body["previous_response_id"] = session_id
        request = urllib.request.Request(
            self.URL, data=json.dumps(body).encode(), method="POST",
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        )
        emit(f"OpenAI {self.model} 호출 시작{' (앞 대화 이어가기)' if session_id else ''}")

        events: queue.Queue = queue.Queue()
        holder: dict[str, Any] = {}

        def pump() -> None:
            try:
                with urllib.request.urlopen(request, timeout=300) as r:
                    holder["resp"] = r
                    for raw in r:
                        line = raw.decode("utf-8", "replace").strip()
                        if line.startswith("data:"):
                            events.put(("data", line[5:].strip()))
            except urllib.error.HTTPError as e:
                events.put(("http", e.code, e.read().decode("utf-8", "replace")))
            except Exception as e:  # 연결 끊김, 시간 초과 등
                events.put(("exc", e))
            finally:
                events.put(None)

        threading.Thread(target=pump, daemon=True).start()
        state: dict[str, Any] = {"id": None, "searches": 0, "fetched": set(), "text": [], "final": None, "error": None}
        deadline = time.monotonic() + TIMEOUT_SECONDS
        while True:
            if cancel.is_set():
                _close(holder)
                raise Cancelled()
            if time.monotonic() > deadline:
                _close(holder)
                raise EngineError("timeout", f"{TIMEOUT_SECONDS // 60}분 안에 끝나지 않아 멈췄습니다.", "‘다시 시도’를 누르세요.")
            try:
                event = events.get(timeout=0.3)
            except queue.Empty:
                continue
            if event is None:
                break
            if event[0] == "http":
                raise _classify(event[1], event[2])
            if event[0] == "exc":
                raise EngineError("engine_failed", f"OpenAI에 연결하지 못했습니다: {event[1]}", "인터넷 연결을 확인하고 ‘다시 시도’를 누르세요.")
            _handle(event[1], state, emit)

        if state["error"]:
            raise state["error"]
        final = state["final"]
        if final is None:
            raise EngineError("engine_failed", "OpenAI 응답이 끝까지 오지 않았습니다.", "‘다시 시도’를 누르세요.")
        try:
            data = json.loads("".join(state["text"]))
        except json.JSONDecodeError:
            raise EngineError("invalid_output", "OpenAI 결과가 정해진 JSON 형식이 아니어서 받아들이지 않았습니다.", "‘다시 시도’를 누르세요.")
        usage = final.get("usage") or {}
        emit(f"OpenAI 호출 끝 · 입력 {usage.get('input_tokens', '?')} / 출력 {usage.get('output_tokens', '?')} 토큰 · 웹 검색 {state['searches']}회")
        return {"data": data, "session_id": final.get("id") or state["id"], "searches": state["searches"], "fetched": state["fetched"]}


def _handle(raw: str, state: dict[str, Any], emit: Emit) -> None:
    from .claude_cli import _norm_url

    try:
        ev = json.loads(raw)
    except json.JSONDecodeError:
        return
    kind = ev.get("type", "")
    if kind == "response.created":
        state["id"] = (ev.get("response") or {}).get("id")
    elif kind == "response.output_item.done":
        item = ev.get("item") or {}
        if item.get("type") == "web_search_call":
            action = item.get("action") or {}
            what = action.get("type")
            if what == "search":
                state["searches"] += 1
                query = action.get("query") or ", ".join(action.get("queries") or [])
                emit(f"검색: {query}" if query else "검색")
            elif what == "open_page":
                url = action.get("url") or ""
                if url:
                    state["fetched"].add(_norm_url(url))
                emit(f"원문 읽기: {url}" if url else "원문 읽기(주소 없음)")
            elif what == "find_in_page":
                emit(f"페이지 안에서 찾기: {action.get('pattern') or action.get('url') or ''}")
        elif item.get("type") == "message":
            for part in item.get("content") or []:
                if part.get("type") == "output_text":
                    state["text"].append(part.get("text") or "")
                elif part.get("type") == "refusal":
                    state["error"] = EngineError("engine_failed", f"OpenAI가 요청을 거절했습니다: {part.get('refusal')}", "주제나 요청을 바꿔 다시 시도하세요.")
    elif kind == "response.completed":
        state["final"] = ev.get("response") or {}
    elif kind in ("response.failed", "response.incomplete", "error"):
        resp = ev.get("response") or {}
        err = resp.get("error") or ev.get("error") or {}
        detail = err.get("message") or ev.get("message") or (resp.get("incomplete_details") or {}).get("reason") or kind
        state["error"] = _billing_error(str(detail), str(err.get("code") or "")) or EngineError(
            "engine_failed", f"OpenAI 호출이 실패했습니다: {detail}", "‘다시 시도’를 누르세요."
        )


def _classify(status: int, text: str) -> EngineError:
    try:
        err = json.loads(text).get("error") or {}
    except json.JSONDecodeError:
        err = {}
    code, message = str(err.get("code") or ""), str(err.get("message") or text[:200])
    if status == 401:
        return EngineError("auth_required", "OpenAI API 키가 틀렸거나 만료됐습니다.", "키를 확인해 환경 변수에 다시 넣고 서버를 다시 켜세요.")
    billing = _billing_error(message, code)
    if billing:
        return billing
    if status == 429:
        return EngineError("rate_limited", "OpenAI 요청 한도에 걸렸습니다(429).", "잠시 뒤 ‘다시 시도’를 누르세요.")
    if "previous_response" in code or ("previous response" in message.lower() and "not found" in message.lower()):
        return EngineError("session_missing", "저장된 이전 대화를 찾지 못했습니다.", "‘다시 시도’를 누르세요.")
    return EngineError("engine_failed", f"OpenAI 호출이 실패했습니다({status}): {message}", "‘다시 시도’를 누르세요.")


def _billing_error(message: str, code: str) -> EngineError | None:
    """크레딧 부족·지출 한도 초과. OpenAI 원문에는 프로젝트 ID 같은 계정 정보가 들어 있어
    공개 화면에 그대로 보여 주지 않고, 다시 시도해도 소용없으니 운영자 할 일을 안내한다."""
    low = message.lower()
    if code == "insufficient_quota" or "spend limit" in low or "billing" in low or "exceeded your current quota" in low:
        return EngineError(
            "billing_limit",
            "OpenAI 계정의 크레딧이나 지출 한도에 도달해 더 호출할 수 없습니다.",
            "서버 운영자가 OpenAI 대시보드에서 한도나 결제 설정을 바꿔야 합니다. 그전에는 다시 시도해도 같습니다.",
        )
    return None


def _strict(schema: dict[str, Any]) -> dict[str, Any]:
    """strict 모드에 맞게, null을 허용하는 객체 타입(["object","null"])을 anyOf로 바꾼다."""
    s = copy.deepcopy(schema)

    def walk(node: Any) -> Any:
        if isinstance(node, dict):
            node = {k: walk(v) for k, v in node.items()}
            t = node.get("type")
            if isinstance(t, list) and "object" in t and "null" in t:
                obj = {**node, "type": "object"}
                return {"anyOf": [obj, {"type": "null"}]}
            return node
        if isinstance(node, list):
            return [walk(x) for x in node]
        return node

    return walk(s)


def _close(holder: dict[str, Any]) -> None:
    resp = holder.get("resp")
    if resp is not None:
        try:
            resp.close()
        except Exception:
            pass
