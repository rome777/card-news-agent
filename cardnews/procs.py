"""외부 CLI(claude, agy) 실행 도우미.

- 인자는 배열로 넘기고 셸을 거치지 않는다.
- 실행 중 취소 신호와 시간 제한을 확인하고, 멈출 때는 하위 프로세스까지 정리한다.
- 실행 중인 PID를 작업 폴더의 proc.pid에 적어 두었다가, 서버가 강제로 꺼진 뒤 다시 켜질 때 정리한다.
"""
from __future__ import annotations

import os
import queue
import subprocess
import threading
import time
from pathlib import Path
from typing import Callable

from .engines import Cancelled, EngineError

PID_FILE = "proc.pid"
_NO_WINDOW = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0


def run_streaming(
    args: list[str],
    cwd: Path,
    cancel: threading.Event,
    timeout: float,
    on_line: Callable[[str], None],
    input_text: str | None = None,
) -> tuple[int, str]:
    """프로세스를 실행하고 표준 출력을 한 줄씩 on_line에 넘긴다. (종료 코드, 표준 오류 끝부분)을 돌려준다."""
    cwd.mkdir(parents=True, exist_ok=True)
    try:
        proc = subprocess.Popen(
            args, cwd=cwd, stdin=subprocess.PIPE if input_text is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, encoding="utf-8", errors="replace",
            creationflags=_NO_WINDOW,
        )
    except OSError as e:
        raise EngineError("engine_unavailable", f"{Path(args[0]).name}을(를) 실행하지 못했습니다: {e}", "설치 상태를 확인하세요.")

    pid_file = cwd / PID_FILE
    pid_file.write_text(str(proc.pid))
    lines: queue.Queue[str | None] = queue.Queue()
    stderr_tail: list[str] = []
    threading.Thread(target=_pump, args=(proc.stdout, lines), daemon=True).start()
    threading.Thread(target=_collect, args=(proc.stderr, stderr_tail), daemon=True).start()
    if input_text is not None:
        try:
            proc.stdin.write(input_text)
            proc.stdin.close()
        except OSError:
            pass

    deadline = time.monotonic() + timeout
    try:
        while True:
            if cancel.is_set():
                kill_tree(proc.pid)
                raise Cancelled()
            if time.monotonic() > deadline:
                kill_tree(proc.pid)
                raise EngineError("timeout", f"{int(timeout // 60)}분 안에 끝나지 않아 멈췄습니다.", "‘다시 시도’를 누르세요.")
            try:
                line = lines.get(timeout=0.3)
            except queue.Empty:
                continue
            if line is None:
                break
            on_line(line)
        try:
            proc.wait(timeout=30)
        except subprocess.TimeoutExpired:
            kill_tree(proc.pid)
    finally:
        if proc.poll() is None:
            kill_tree(proc.pid)
        pid_file.unlink(missing_ok=True)
    return proc.returncode if proc.returncode is not None else -1, "".join(stderr_tail)


def reap_orphans(data_dir: Path) -> int:
    """지난번 서버가 강제로 꺼질 때 남은 claude/agy 프로세스를 정리한다."""
    killed = 0
    for pid_file in (data_dir / "projects").glob(f"*/*/{PID_FILE}"):
        try:
            pid = int(pid_file.read_text().strip())
        except ValueError:
            pid = 0
        name = process_name(pid).lower() if pid else ""
        if name.startswith(("claude", "agy")):
            kill_tree(pid)
            killed += 1
        pid_file.unlink(missing_ok=True)
    return killed


def kill_tree(pid: int) -> None:
    if os.name == "nt":
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(pid)], capture_output=True, creationflags=_NO_WINDOW)
    else:
        try:
            os.kill(pid, 9)
        except OSError:
            pass


def process_name(pid: int) -> str:
    if os.name != "nt":
        return ""
    out = subprocess.run(
        ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
        capture_output=True, text=True, creationflags=_NO_WINDOW,
    ).stdout.strip()
    return out.split(",")[0].strip('"') if out.startswith('"') else ""


def _pump(stream, out: queue.Queue) -> None:
    for line in stream:
        out.put(line)
    out.put(None)


def _collect(stream, tail: list[str]) -> None:
    for line in stream:
        tail.append(line)
        del tail[:-40]
