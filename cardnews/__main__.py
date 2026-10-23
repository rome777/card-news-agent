"""실행: python -m cardnews [--engine openai|claude|mock] [--port 8765] [--data-dir data] [--host 0.0.0.0 --public]"""
from __future__ import annotations

import argparse
import logging
import socket
import sys
from pathlib import Path

APP_ROOT = Path(__file__).resolve().parent.parent


def main() -> int:
    # 서버 창(또는 Docker 로그)에 안내가 바로 보이게 한다
    sys.stdout.reconfigure(line_buffering=True)
    parser = argparse.ArgumentParser(prog="python -m cardnews", description="카드뉴스 에이전트 웹앱")
    parser.add_argument("--engine", choices=["openai", "claude", "mock"], default="openai", help="조사·원고 엔진 (기본: openai)")
    parser.add_argument("--host", default="127.0.0.1", help="받을 주소 (기본: 127.0.0.1, 서버 배포 때는 0.0.0.0)")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--data-dir", type=Path, default=APP_ROOT / "data", help="결과를 저장할 폴더 (기본: 앱 폴더의 data)")
    parser.add_argument("--openai-model", default="gpt-5.4-mini", help="--engine openai일 때 모델 (기본: gpt-5.4-mini)")
    parser.add_argument("--openai-effort", choices=["low", "medium", "high"], default="medium", help="--engine openai일 때 reasoning effort (기본: medium)")
    parser.add_argument(
        "--images", choices=["openai", "agy", "mock"],
        help="그림 작업자 (기본: openai·claude 엔진이면 openai, mock 엔진이면 mock). agy는 한도가 작아 옵션으로만 둔다",
    )
    parser.add_argument("--openai-image-model", default="gpt-image-1.5", help="--images openai일 때 모델 (기본: gpt-image-1.5)")
    parser.add_argument("--openai-image-quality", choices=["low", "medium", "high"], default="medium", help="--images openai일 때 품질 (기본: medium)")
    parser.add_argument("--public", action="store_true", help="공개 서버 모드: 다른 사람의 프로젝트 목록을 보여 주지 않고 하루 사용량 상한을 건다")
    parser.add_argument("--daily-project-limit", type=int, default=30, help="공개 모드에서 하루에 새로 만들 수 있는 프로젝트 수 (0이면 제한 없음)")
    parser.add_argument("--ip-daily-project-limit", type=int, default=5, help="공개 모드에서 접속 주소 하나가 하루에 만들 수 있는 프로젝트 수 (0이면 제한 없음)")
    parser.add_argument("--daily-image-limit", type=int, default=150, help="공개 모드에서 하루에 만들 수 있는 그림 수 (0이면 제한 없음)")
    parser.add_argument(
        "--mock-fail", choices=["research", "deep_research", "storyboard", "images"],
        help="모의 실행에서 이 단계를 처음 한 번 실패시킨다(images는 모의 그림 작업자의 첫 그림을 할당량 초과로 실패시킴)",
    )
    parser.add_argument("--model", default="sonnet", help="--engine claude일 때 Claude CLI 모델 (기본: sonnet)")
    parser.add_argument("--effort", default="medium", help="--engine claude일 때 Claude CLI effort (기본: medium)")
    args = parser.parse_args()

    if not _port_free(args.host, args.port):
        print(f"{args.port}번 포트를 이미 다른 프로그램이 쓰고 있습니다.")
        print(f"다른 포트로 켜려면: uv run python -m cardnews --port {args.port + 1}")
        return 1

    import uvicorn

    from .engines.claude_cli import ClaudeCliEngine
    from .engines.mock import MockEngine
    from .engines.openai_api import OpenAIEngine
    from .images import AgyImageMaker, MockImageMaker, OpenAIImageMaker
    from .procs import reap_orphans
    from .render import Renderer
    from .server import Limits, create_app
    from .store import Store
    from .worker import Worker

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    store = Store(args.data_dir.resolve())
    if args.engine == "openai":
        engine = OpenAIEngine(model=args.openai_model, effort=args.openai_effort)
    elif args.engine == "claude":
        engine = ClaudeCliEngine(model=args.model, effort=args.effort)
    else:
        engine = MockEngine(fail_once=args.mock_fail if args.mock_fail != "images" else None)
    image_tool = args.images or ("mock" if args.engine == "mock" else "openai")
    if image_tool == "agy":
        images = AgyImageMaker()
    elif image_tool == "openai":
        images = OpenAIImageMaker(model=args.openai_image_model, quality=args.openai_image_quality)
    else:
        images = MockImageMaker(fail_once=args.mock_fail == "images")
    for problem in (engine.check() if hasattr(engine, "check") else None, images.check()):
        if problem:
            print(f"주의: {problem} 해당 단계를 시작하면 실패로 표시됩니다.")
    reaped = reap_orphans(store.data_dir)
    if reaped:
        print(f"지난번 서버가 꺼질 때 남은 claude/agy 프로세스 {reaped}개를 정리했습니다.")
    limits = Limits(args.daily_project_limit, args.ip_daily_project_limit, args.daily_image_limit) if args.public else Limits()
    worker = Worker(store, engine, images, Renderer(), image_quota=limits.take_image)
    interrupted = worker.recover_interrupted()
    if interrupted:
        print(f"지난번 서버가 꺼질 때 진행 중이던 작업 {interrupted}개를 '중단됨'으로 표시했습니다.")

    print(f"엔진: {engine.name}{f' ({engine.model})' if hasattr(engine, 'model') else ''} · 그림: {image_tool} · 저장 폴더: {store.data_dir}")
    if args.public:
        print(f"공개 모드: 하루 프로젝트 {args.daily_project_limit or '무제한'}개(주소당 {args.ip_daily_project_limit or '무제한'}개), 그림 {args.daily_image_limit or '무제한'}장")
    shown = "127.0.0.1" if args.host in ("0.0.0.0", "::") else args.host
    print(f"브라우저에서 http://{shown}:{args.port} 를 여세요. 끄려면 이 창에서 Ctrl+C")
    uvicorn.run(create_app(store, worker, public=args.public, limits=limits), host=args.host, port=args.port, log_level="warning")
    return 0


def _port_free(host: str, port: int) -> bool:
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    with socket.socket(family, socket.SOCK_STREAM) as s:
        try:
            s.bind((host, port))
        except OSError:
            return False
    return True


if __name__ == "__main__":
    sys.exit(main())
