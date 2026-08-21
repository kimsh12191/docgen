#!/usr/bin/env python3
"""docgen CLI: doctor / render / build."""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from config import load_config
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
        renderer.health()
        print(f"OK {cfg.renderer.url}")
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

    # The renderer is a hard dependency of the loop: do not start without it.
    renderer = RendererClient(
        base_url=cfg.renderer.url,
        timeout=cfg.renderer.timeout,
        device_scale=cfg.renderer.device_scale,
    )
    try:
        renderer.health()
    except RendererError as exc:
        print(f"오류: renderer health 검사에 실패해서 빌드를 시작하지 않습니다: {exc}", file=sys.stderr)
        return 1

    try:
        summary = Pipeline(cfg, out_dir).build(source)
    except (RendererError, LLMError, RuntimeError, FileNotFoundError) as exc:
        LOG.error("build failed: %s", exc)
        print(f"빌드 실패: {exc}", file=sys.stderr)
        return 1

    stop = STOP_REASON_KO.get(summary["stop_reason"], summary["stop_reason"])
    print(f"clone.html : {summary['clone_html']}")
    print(f"clone.png  : {summary['clone_png']}")
    print(f"종료 사유   : {stop}")
    print(
        "라운드     : 총 {rounds_run}회 / 반영(keep) {kept} / 되돌림(revert) {reverted} / "
        "거부(reject) {rejected} / 오류 {errors}".format(**summary)
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
    build.set_defaults(func=cmd_build)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    setup_logging(args.verbose)
    cfg = load_config(args.config)
    return args.func(args, cfg)


if __name__ == "__main__":
    sys.exit(main())
