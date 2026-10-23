"""실행 엔진 공통 규약.

엔진은 execute(task, session_id, emit, cancel) 하나만 구현한다.
- task: 앱이 만든 작업 설명(stage, 주제, 선택한 후보, 지금까지의 답변 등)
- session_id: 같은 프로젝트에서 앞서 받은 엔진 세션 ID. 처음이면 None
- emit(message): 진행 내역 한 줄을 남긴다
- cancel: threading.Event. 설정되면 Cancelled를 올리고 하위 작업을 정리한다

결과는 Outcome 하나로 정규화한다. kind는 result / need_input 중 하나이고,
실패는 EngineError로 올린다. 이 규약은 이 앱이 정한 것이며 CLI 공통 규격이 아니다.
"""
from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol

Emit = Callable[[str], None]


@dataclass
class Outcome:
    kind: str  # "result" | "need_input"
    data: dict[str, Any] = field(default_factory=dict)
    session_id: str | None = None


class EngineError(Exception):
    """사용자에게 보여 줄 실패. code로 인증·검색·출력 오류 등을 구분한다."""

    def __init__(self, code: str, message: str, next_action: str):
        super().__init__(message)
        self.code = code
        self.message = message
        self.next_action = next_action

    def as_dict(self) -> dict[str, str]:
        return {"code": self.code, "message": self.message, "next_action": self.next_action}


class Cancelled(Exception):
    pass


class Engine(Protocol):
    name: str
    is_mock: bool

    def execute(
        self, task: dict[str, Any], session_id: str | None, emit: Emit, cancel: threading.Event
    ) -> Outcome: ...
