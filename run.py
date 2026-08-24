#!/usr/bin/env python3
"""docgen CLI: doctor / render / build."""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from config import THINKING_MODES, load_config
from llm import LLMError, QwenClient, image_part, system_message, user_message
from renderer import RendererClient, RendererError
from utils import ensure_dir, read_text, setup_logging, write_bytes, write_json

LOG = logging.getLogger("docgen.cli")

STOP_REASON_KO = {
    "done": "DONE (VERIFY가 완료로 판단)",
    "max_rounds": "최대 라운드 도달",
}


# ---------------------------------------------------------------------- doctor

def cmd_doctor(args, cfg) -> int:
    failures: list[str] = []

    print("[LLM]")
    client = QwenClient(cfg.llm)
    try:
        models = client.list_models()
        if cfg.llm.model in models:
            print(f"OK {cfg.llm.model}")
        elif models:
            print(f"FAIL 모델 {cfg.llm.model!r} 을(를) 서비스하지 않습니다. 사용 가능: {', '.join(models[:10])}")
            failures.append("llm-model")
        else:
            print(f"FAIL /models 가 모델 목록을 반환하지 않았습니다 ({cfg.llm.base_url})")
            failures.append("llm-model")
    except LLMError as exc:
        print(f"FAIL {exc}")
        failures.append("llm")

    print("[Renderer]")
    renderer = RendererClient(
        base_url=cfg.renderer.url,
        timeout=cfg.renderer.timeout,
        device_scale=cfg.renderer.device_scale,
    )
    renderer_up = False
    try:
        info = renderer.health()
        status = str(info.get("status", "")).strip()
        # The status line carries the Chromium build and installed fonts; worth
        # seeing, and the only place an odd 200 response would show up.
        print(f"OK {cfg.renderer.url}" + (f"  {status[:160]}" if status else ""))
        renderer_up = True
    except RendererError as exc:
        print(f"FAIL {exc}")
        failures.append("renderer")

    print("[Renderer probe]")
    if not renderer_up:
        print("SKIP renderer health 검사가 실패해서 건너뜁니다")
        failures.append("renderer-probe")
    else:
        try:
            png, metrics = renderer.smoke(width=cfg.renderer.width, wait_ms=cfg.renderer.wait_ms)
            ensure_dir("tmp")
            png_path = write_bytes("tmp/renderer_smoke.png", png)
            write_json("tmp/renderer_metrics.json", metrics)
            print(f"OK {png_path}")
        except RendererError as exc:
            print(f"FAIL {exc}")
            failures.append("renderer-probe")

    if args.llm_image:
        print("[LLM 멀티모달]")
        try:
            from PIL import Image

            ensure_dir("tmp")
            probe_img = Path("tmp/llm_probe.png")
            Image.new("RGB", (64, 32), (255, 255, 255)).save(probe_img)
            resp = client.chat(
                [
                    system_message("You answer in one word."),
                    user_message("What colour fills this image?", image_part(probe_img, cfg.llm.image_max_side)),
                ],
                thinking=False,
                max_tokens=32,
                stage="doctor",
            )
            print(f"OK {resp.content.strip()[:60]!r}")
            if not client.supports_thinking_flag:
                print("WARN 서버가 chat_template_kwargs 를 거부했습니다. stage별 thinking 제어가 비활성화됩니다")
        except (LLMError, OSError) as exc:
            print(f"FAIL {exc}")
            failures.append("llm-multimodal")

    if failures:
        print(f"실패한 검사: {', '.join(failures)}")
        return 1
    print("모든 검사를 통과했습니다.")
    return 0


# ---------------------------------------------------------------------- render

def cmd_render(args, cfg) -> int:
    html_path = Path(args.html)
    if not html_path.exists():
        print(f"오류: {html_path} 파일을 찾을 수 없습니다", file=sys.stderr)
        return 1

    renderer = RendererClient(
        base_url=cfg.renderer.url,
        timeout=cfg.renderer.timeout,
        device_scale=cfg.renderer.device_scale,
    )
    try:
        png, metrics = renderer.probe(
            read_text(html_path),
            width=args.width or cfg.renderer.width,
            wait_ms=cfg.renderer.wait_ms,
        )
    except RendererError as exc:
        print(f"렌더 실패: {exc}", file=sys.stderr)
        return 1

    out = Path(args.output or html_path.with_suffix(".png"))
    write_bytes(out, png)
    metrics_path = out.with_name(out.stem + "_metrics.json")
    write_json(metrics_path, metrics)
    print(f"{out} ({len(png)} 바이트)")
    print(f"{metrics_path}")
    return 0


# ----------------------------------------------------------------------- build

def _operator_notes(args) -> str:
    """Collect --note / --notes-file into one block of operator context."""
    parts: list[str] = []
    if args.notes_file:
        path = Path(args.notes_file)
        if not path.exists():
            raise FileNotFoundError(f"메모 파일을 찾을 수 없습니다: {path}")
        parts.append(read_text(path).strip())
    for note in args.note or []:
        parts.append(note.strip())
    return "\n".join(p for p in parts if p)


def cmd_build(args, cfg) -> int:
    from pipeline import Pipeline

    source = Path(args.source)
    if not source.exists():
        print(f"오류: {source} 파일을 찾을 수 없습니다", file=sys.stderr)
        return 1

    out_dir = ensure_dir(args.output or (Path("out") / source.stem))
    setup_logging(args.verbose, logfile=out_dir / "run.log")

    if args.max_rounds:
        cfg.loop.max_rounds = args.max_rounds
    if args.bootstrap:
        cfg.bootstrap.staged = args.bootstrap == "staged"
    if args.thinking:
        cfg.llm.thinking = args.thinking
    if args.log_chars is not None:
        # -1 is "do not print them at all"; 0 is "print all of it".
        cfg.llm.log_calls = args.log_chars >= 0
        cfg.llm.log_chars = max(0, args.log_chars)

    try:
        notes = _operator_notes(args)
    except FileNotFoundError as exc:
        print(f"오류: {exc}", file=sys.stderr)
        return 1

    interactive = args.interactive
    # Pipeline resolves None the same way; resolved here too for the guard below.
    verify_mode = args.verify or ("both" if interactive else "model")
    tty = sys.stdin.isatty()

    # The UI exists to collect operator input, so turning it on means asking for
    # it at both stages. Blank answer -> the model's own judgement is used.
    if args.ui:
        interactive = True
        if args.verify is None:
            verify_mode = "both"
        tty = True  # the browser supplies the input, not the terminal

    if interactive and not tty:
        # A blocked input() in a batch run would hang the whole build.
        print("경고: 대화형 입력을 받을 수 없는 환경이라 --interactive 를 끕니다",
              file=sys.stderr)
        interactive = False
        if verify_mode == "both":
            verify_mode = "model"
    if notes:
        print(f"운영자 메모 {len(notes)}자를 PLAN/VERIFY 프롬프트에 넣습니다")
    if interactive:
        print("대화형 모드: 매 라운드 PLAN에서 지시를 덧붙일 수 있습니다")
    if verify_mode == "both":
        print("VERIFY: Qwen 판정을 보고 사람이 갈아치울 수 있습니다")

    # The renderer is a hard dependency of the loop: do not start without it.
    renderer = RendererClient(
        base_url=cfg.renderer.url,
        timeout=cfg.renderer.timeout,
        device_scale=cfg.renderer.device_scale,
    )
    try:
        status = str(renderer.health().get("status", "")).strip()
        if status:
            print(f"renderer: {status[:160]}")
    except RendererError as exc:
        print(f"오류: renderer health 검사에 실패해서 빌드를 시작하지 않습니다: {exc}", file=sys.stderr)
        return 1

    server = None
    prompter = None
    if args.ui:
        from ui import ReviewServer

        server = ReviewServer(
            out_dir,
            port=args.ui_port,
            timeout=args.ui_timeout,
            host=args.ui_host,
            public_host=args.ui_public_host,
        )
        url = server.start()
        prompter = server
        print(f"검토 UI: {url}  (브라우저에서 열어두세요)")
        for note in server.access_notes:
            print(f"  주의: {note}")

    try:
        summary = Pipeline(
            cfg,
            out_dir,
            notes=notes,
            interactive=interactive,
            verify_mode=verify_mode,
            prompter=prompter,
        ).build(source)
    except (RendererError, LLMError, RuntimeError, FileNotFoundError) as exc:
        LOG.error("build failed: %s", exc)
        print(f"빌드 실패: {exc}", file=sys.stderr)
        return 1
    finally:
        if server is not None:
            server.stop()

    stop = STOP_REASON_KO.get(summary["stop_reason"], summary["stop_reason"])
    print(f"clone.html : {summary['clone_html']}")
    print(f"clone.png  : {summary['clone_png']}")
    print(f"종료 사유   : {stop}")
    print(
        "라운드     : 총 {rounds_run}회 / 반영(keep) {kept} / 되돌림(revert) {reverted} / "
        "거부(reject) {rejected} / 오류 {errors}".format(**summary)
    )
    if summary["operator_interventions"] or summary["skipped"]:
        print(
            "사람 개입   : {operator_interventions}회 "
            "({operator_rounds}개 라운드, 건너뜀 {skipped}개) "
            "— 모델 단독 성능 평가에서 제외할 라운드입니다".format(**summary)
        )
    if not summary["thinking_control"]:
        print("주의: 서버가 chat_template_kwargs 를 거부해서 stage별 thinking 제어 없이 실행되었습니다")
    return 0


# ------------------------------------------------------------------------ main

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="run.py",
        description="문서 PNG -> 편집 가능한 HTML 클론",
    )
    parser.add_argument("-c", "--config", help="config.toml 경로")
    parser.add_argument("-v", "--verbose", action="store_true", help="상세 로그 출력")
    sub = parser.add_subparsers(dest="command", required=True)

    doctor = sub.add_parser("doctor", help="LLM과 renderer 서비스 상태를 점검한다")
    doctor.add_argument(
        "--llm-image",
        action="store_true",
        help="작은 이미지를 실제로 보내 멀티모달 호출까지 확인한다",
    )
    doctor.set_defaults(func=cmd_doctor)

    render = sub.add_parser("render", help="HTML 파일을 renderer로 PNG로 렌더한다")
    render.add_argument("html", help="렌더할 HTML 파일")
    render.add_argument("-o", "--output", help="출력 PNG 경로")
    render.add_argument("--width", type=int, help="렌더 폭 (기본값은 config)")
    render.set_defaults(func=cmd_render)

    build = sub.add_parser("build", help="문서 PNG로부터 편집 가능한 HTML 클론을 만든다")
    build.add_argument("source", help="입력 문서 PNG")
    build.add_argument("-o", "--output", help="출력 디렉터리 (기본값 out/<이름>)")
    build.add_argument("--max-rounds", type=int, help="최대 라운드 수 (기본값은 config)")
    build.add_argument(
        "--log-chars",
        type=int,
        default=None,
        help=("터미널에 찍는 프롬프트·응답 한 덩어리의 최대 길이 (기본 2000). "
              "0 이면 전부 찍는다. -1 이면 프롬프트·응답을 아예 찍지 않고 "
              "out/<이름>/llm/ 파일에만 남긴다"),
    )
    build.add_argument(
        "--thinking",
        choices=THINKING_MODES,
        default=None,
        help=("어느 단계에서 thinking을 켤지. all=전부(기본), "
              "judging=판단 단계만(PLAN/육안대조/VERIFY). "
              "생성 단계에서 켜면 reasoning이 max_tokens를 HTML과 나눠 쓴다 "
              "(env: DOCGEN_LLM_THINKING)"),
    )
    build.add_argument(
        "--bootstrap",
        choices=("staged", "single"),
        default=None,
        help=("첫 HTML 생성 방식. staged=구조 → 육안 대조 → 블록별 채우기(기본), "
              "single=한 번의 호출로 전체 생성. 기본값은 config의 bootstrap.staged"),
    )
    build.add_argument(
        "--note",
        action="append",
        help="PLAN/VERIFY에 넣을 운영자 메모. 여러 번 쓸 수 있다",
    )
    build.add_argument("--notes-file", help="운영자 메모를 담은 텍스트 파일")
    build.add_argument(
        "--interactive",
        action="store_true",
        help="매 라운드 PLAN에 지시를 덧붙인다 (VERIFY는 --verify both 가 된다)",
    )
    build.add_argument(
        "--ui",
        action="store_true",
        help="로컬 웹 UI로 개입한다. 비교 이미지를 보면서 버튼으로 판정한다",
    )
    build.add_argument("--ui-port", type=int, default=0, help="UI 포트 (기본: 임의 포트)")
    build.add_argument(
        "--ui-host",
        default="127.0.0.1",
        help="UI 바인딩 주소. 다른 PC(예: 윈도우)에서 접속하려면 0.0.0.0",
    )
    build.add_argument(
        "--ui-public-host",
        default=None,
        help=(
            "UI 주소를 찍을 때 쓸 호스트. 바인딩은 --ui-host 그대로다. "
            "컨테이너 안에서는 자기 주소를 알 수 없으니 서버 IP를 여기에 준다 "
            "(env: DOCGEN_UI_PUBLIC_HOST)"
        ),
    )
    build.add_argument(
        "--ui-timeout",
        type=int,
        default=1800,
        help="UI 응답 대기 시간(초). 넘으면 입력 없음으로 처리한다",
    )
    build.add_argument(
        "--verify",
        choices=("model", "both"),
        help=(
            "VERIFY에서 사람에게 물을지. model=묻지 않음(기본), "
            "both=Qwen 판정을 보여주고 사람이 갈아치울 수 있음"
        ),
    )
    build.set_defaults(func=cmd_build)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    setup_logging(args.verbose)
    cfg = load_config(args.config)
    return args.func(args, cfg)


if __name__ == "__main__":
    sys.exit(main())
