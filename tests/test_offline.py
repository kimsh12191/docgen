"""Offline end-to-end exercise of the docgen pipeline against test doubles.

Run: python3 tests/test_offline.py
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

import mock_services  # noqa: E402
from PIL import Image, ImageDraw  # noqa: E402

import utils  # noqa: E402
from config import load_config  # noqa: E402
from llm import QwenClient, image_part, system_message, user_message  # noqa: E402
from renderer import RendererClient  # noqa: E402

PASS: list[str] = []


def ok(label: str) -> None:
    PASS.append(label)
    print(f"  PASS {label}")


def make_source_png(path: Path) -> Path:
    img = Image.new("RGB", (800, 560), (255, 255, 255))
    d = ImageDraw.Draw(img)
    d.text((40, 36), "Quarterly Expense Report", fill=(0, 0, 0))
    d.text((40, 70), "Prepared for the finance committee.", fill=(40, 40, 40))
    top, left, cw, rh = 110, 40, 160, 28
    for r in range(4):
        for c in range(3):
            box = [left + c * cw, top + r * rh, left + (c + 1) * cw, top + (r + 1) * rh]
            d.rectangle(box, outline=(0, 0, 0))
    for c, head in enumerate(["Item", "Q1", "Q2"]):
        d.text((left + c * cw + 8, top + 8), head, fill=(0, 0, 0))
    rows = [("Travel", "1,200", "1,450"), ("Equipment", "3,400", "2,900"), ("Total", "4,600", "4,350")]
    for r, row in enumerate(rows, start=1):
        for c, cell in enumerate(row):
            d.text((left + c * cw + 8, top + r * rh + 8), cell, fill=(0, 0, 0))
    d.text((40, 240), "Notes: figures are provisional and subject to audit.", fill=(40, 40, 40))
    utils.ensure_dir(path.parent)
    img.save(path)
    return path


# --------------------------------------------------------------- 1. renderer

def test_renderer_smoke() -> str:
    print("[1] renderer client smoke test")
    _, url = mock_services.start(mock_services.RendererHandler)
    client = RendererClient(base_url=url, timeout=20, device_scale=1.0)

    health = client.health()
    assert health.get("ok") is True, health
    ok("GET /health")

    png, metrics = client.smoke(width=800, wait_ms=400)
    assert png[:8] == b"\x89PNG\r\n\x1a\n", "not a PNG"
    utils.write_bytes("tmp/renderer_smoke.png", png)
    utils.write_json("tmp/renderer_metrics.json", metrics)
    assert Path("tmp/renderer_smoke.png").exists()
    assert Path("tmp/renderer_metrics.json").exists()
    w, h = utils.image_size(png)
    assert w == 800, w
    ok(f"POST /probe -> tmp/renderer_smoke.png ({w}x{h}, {len(png)} bytes)")

    assert "page" in metrics and "elements" in metrics, metrics.keys()
    texts = [e["text"] for e in metrics["elements"]]
    assert any("renderer smoke test" in t for t in texts), texts
    ok("metrics carry page box + element bboxes")

    # Failure contract: ok:false must raise, not return junk.
    try:
        client.probe("", width=800)
    except Exception as exc:  # RendererError
        assert "empty html" in str(exc), exc
        ok("ok:false raises RendererError")
    else:
        raise AssertionError("empty html should have raised")
    return url


# -------------------------------------------------------------------- 2. llm

def test_llm_smoke() -> str:
    print("[2] LLM client smoke test")
    _, url = mock_services.start(mock_services.LLMHandler)
    base = f"{url}/v1"
    cfg = load_config()
    cfg.llm.base_url = base
    cfg.llm.timeout = 20
    client = QwenClient(cfg.llm)

    models = client.list_models()
    assert cfg.llm.model in models, models
    ok(f"GET /models -> {models}")

    img = make_source_png(Path("tmp/llm_probe.png"))
    resp = client.chat(
        [system_message("s"), user_message("Write HTML that recreates it", image_part(img, 1600))],
        thinking=False,
        stage="test",
    )
    html = utils.clean_html_output(resp.content)
    assert html.lower().startswith("<!doctype"), html[:80]
    ok("multimodal chat + fence stripping")

    resp = client.chat(
        [user_message("Find the single most important mismatch", image_part(img), image_part(img))],
        thinking=True,
        stage="test",
    )
    assert "<think>" not in resp.content, resp.content[:80]
    plan = utils.extract_json(resp.content)
    assert plan["scope"] == "global", plan
    ok("<think> stripped, JSON extracted")
    return base


def test_thinking_fallback() -> None:
    print("[3] chat_template_kwargs 400 fallback")
    mock_services.LLMHandler.reject_thinking = True
    try:
        _, url = mock_services.start(mock_services.LLMHandler)
        cfg = load_config()
        cfg.llm.base_url = f"{url}/v1"
        cfg.llm.timeout = 20
        client = QwenClient(cfg.llm)
        assert client.supports_thinking_flag is True
        resp = client.chat([user_message("hello")], thinking=True, stage="test")
        assert resp.content == "ok", resp.content
        assert client.supports_thinking_flag is False, "flag should be disabled after the 400"
        ok("400 -> key dropped, retried once, flag disabled")
    finally:
        mock_services.LLMHandler.reject_thinking = False


# ----------------------------------------------------------------- 4. doctor

def test_doctor(llm_base: str, renderer_url: str) -> None:
    print("[4] doctor")
    import run

    os.environ["DOCGEN_LLM_BASE_URL"] = llm_base
    os.environ["DOCGEN_RENDERER_URL"] = renderer_url
    code = run.main(["doctor", "--llm-image"])
    assert code == 0, f"doctor exited {code}"
    ok("doctor exit 0 with all services up")

    os.environ["DOCGEN_RENDERER_URL"] = "http://127.0.0.1:1"
    code = run.main(["-c", str(ROOT / "tests" / "fast.toml"), "doctor"])
    assert code == 1, f"doctor should fail, exited {code}"
    ok("doctor exit 1 when the renderer is down")
    os.environ["DOCGEN_RENDERER_URL"] = renderer_url


# ------------------------------------------------------------------ 5. build

def test_build(llm_base: str, renderer_url: str) -> None:
    print("[5] full build loop")
    mock_services.LLMHandler.plan_calls = 0
    cfg = load_config()
    cfg.llm.base_url = llm_base
    cfg.llm.timeout = 30
    cfg.renderer.url = renderer_url
    cfg.renderer.timeout = 30
    cfg.loop.max_rounds = 8

    from pipeline import Pipeline

    out = ROOT / "out" / "offline_test"
    if out.exists():
        import shutil

        shutil.rmtree(out)
    source = make_source_png(Path("tmp/source_fixture.png"))
    summary = Pipeline(cfg, out).build(source)

    for rel in ("clone.html", "clone.png", "final_verify.json", "summary.json",
                "rounds/bootstrap.html", "rounds/bootstrap.png"):
        assert (out / rel).exists(), f"missing {rel}"
    ok("clone.html / clone.png / final_verify.json / bootstrap.* written")

    decisions = [r["decision"] for r in summary["rounds"]]
    assert decisions == ["keep", "rejected", "revert", "done"], decisions
    ok(f"decision sequence {decisions}")
    assert summary["stop_reason"] == "done", summary["stop_reason"]
    ok("loop stopped on DONE")

    for rel in ("plan.json", "action_raw.txt", "before.html", "before.png",
                "candidate.html", "candidate.png", "metrics.json", "verify.json"):
        assert (out / "rounds" / "r01" / rel).exists(), f"missing r01/{rel}"
    ok("round r01 has the full artefact set")

    # Rejected round: no candidate render, an error record instead.
    r02 = out / "rounds" / "r02"
    assert (r02 / "error.json").exists()
    assert not (r02 / "candidate.png").exists()
    assert json.loads((r02 / "error.json").read_text())["stage"] == "apply"
    ok("rejected round recorded, no candidate render")

    # REVERT must not advance current state.
    r03, r04 = out / "rounds" / "r03", out / "rounds" / "r04"
    assert (r03 / "before.html").read_text() == (r04 / "before.html").read_text(), \
        "revert leaked the rejected candidate into the next round"
    ok("REVERT restored the previous HTML")

    # DONE must adopt the candidate.
    assert (out / "clone.html").read_text() == (r04 / "candidate.html").read_text()
    ok("DONE adopted the candidate as clone.html")

    clone = (out / "clone.html").read_text()
    okc, reason = utils.html_sanity_check(clone)
    assert okc, reason
    assert "<table" in clone and "Quarterly Expense Report" in clone
    ok(f"clone.html is valid, editable HTML ({len(clone)} chars)")

    verify = json.loads((out / "final_verify.json").read_text())
    assert verify["decision"] == "done", verify
    ok("final_verify.json holds the terminal verdict")

    # History must stay short and be labelled as history, not fact.
    import prompts
    block = prompts.history_block([f"Round {i}: g -> keep" for i in range(1, 7)])
    assert block.count("- Round") == 3, block
    assert "history, not facts" in block
    ok("PLAN history limited to 3 entries with the history caveat")


def main() -> int:
    utils.setup_logging(verbose=False)
    (ROOT / "tests" / "fast.toml").write_text(
        (ROOT / "config.toml").read_text().replace("timeout = 900", "timeout = 5").replace("timeout = 180", "timeout = 5")
    )
    renderer_url = test_renderer_smoke()
    llm_base = test_llm_smoke()
    test_thinking_fallback()
    test_doctor(llm_base, renderer_url)
    test_build(llm_base, renderer_url)
    print(f"\n{len(PASS)} checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
