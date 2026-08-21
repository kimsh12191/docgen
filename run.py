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
            print(f"FAIL model {cfg.llm.model!r} not served; available: {', '.join(models[:10])}")
            failures.append("llm-model")
        else:
            print(f"FAIL /models returned no models ({cfg.llm.base_url})")
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
        print("SKIP renderer health failed")
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
        print("[LLM multimodal]")
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
                print("WARN server rejected chat_template_kwargs; per-stage thinking control is off")
        except (LLMError, OSError) as exc:
            print(f"FAIL {exc}")
            failures.append("llm-multimodal")

    if failures:
        print(f"FAILED checks: {', '.join(failures)}")
        return 1
    print("All checks passed.")
    return 0


# ---------------------------------------------------------------------- render

def cmd_render(args, cfg) -> int:
    html_path = Path(args.html)
    if not html_path.exists():
        print(f"error: {html_path} not found", file=sys.stderr)
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
        print(f"render failed: {exc}", file=sys.stderr)
        return 1

    out = Path(args.output or html_path.with_suffix(".png"))
    write_bytes(out, png)
    metrics_path = out.with_name(out.stem + "_metrics.json")
    write_json(metrics_path, metrics)
    print(f"{out} ({len(png)} bytes)")
    print(f"{metrics_path}")
    return 0


# ----------------------------------------------------------------------- build

def cmd_build(args, cfg) -> int:
    from pipeline import Pipeline

    source = Path(args.source)
    if not source.exists():
        print(f"error: {source} not found", file=sys.stderr)
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
        print(f"error: renderer health check failed, refusing to build: {exc}", file=sys.stderr)
        return 1

    try:
        summary = Pipeline(cfg, out_dir).build(source)
    except (RendererError, LLMError, RuntimeError, FileNotFoundError) as exc:
        LOG.error("build failed: %s", exc)
        print(f"build failed: {exc}", file=sys.stderr)
        return 1

    print(f"clone.html      {summary['clone_html']}")
    print(f"clone.png       {summary['clone_png']}")
    print(f"stop_reason     {summary['stop_reason']}")
    print(
        "rounds          {rounds_run} run, {kept} kept, {reverted} reverted, "
        "{rejected} rejected, {errors} errors".format(**summary)
    )
    return 0


# ------------------------------------------------------------------------ main

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="run.py", description="Document PNG -> editable HTML clone")
    parser.add_argument("-c", "--config", help="path to config.toml")
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)

    doctor = sub.add_parser("doctor", help="check the LLM and renderer services")
    doctor.add_argument(
        "--llm-image",
        action="store_true",
        help="also send a tiny image to the VLM to verify multimodal calls",
    )
    doctor.set_defaults(func=cmd_doctor)

    render = sub.add_parser("render", help="render an HTML file to PNG via the renderer")
    render.add_argument("html")
    render.add_argument("-o", "--output")
    render.add_argument("--width", type=int)
    render.set_defaults(func=cmd_render)

    build = sub.add_parser("build", help="build an editable HTML clone of a document PNG")
    build.add_argument("source")
    build.add_argument("-o", "--output")
    build.add_argument("--max-rounds", type=int)
    build.set_defaults(func=cmd_build)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    setup_logging(args.verbose)
    cfg = load_config(args.config)
    return args.func(args, cfg)


if __name__ == "__main__":
    sys.exit(main())
