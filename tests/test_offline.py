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

import prompts  # noqa: E402
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
    assert plan["scope"] in ("global", "local"), plan
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

        # The fallback retry must be extra, not taken from the retry budget,
        # so it still happens when retries is 1.
        cfg2 = load_config()
        cfg2.llm.base_url = f"{url}/v1"
        cfg2.llm.timeout = 20
        cfg2.llm.retries = 1
        client2 = QwenClient(cfg2.llm)
        resp2 = client2.chat([user_message("hello")], thinking=True, stage="test")
        assert resp2.content == "ok", resp2.content
        assert client2.supports_thinking_flag is False
        ok("fallback still retries when retries=1")
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




# ------------------------------------------------- 6. truncation failure modes

def test_truncation(llm_base: str, renderer_url: str) -> None:
    print("[6] ACTION truncation handling")
    import shutil

    from pipeline import HTML_WARN_SIZE, Pipeline

    # A document larger than the old 60000-char input cap.
    filler = "".join(
        f'<tr><td>{i}</td><td>Line item description number {i} for the ledger</td>'
        f'<td>{i * 7}</td></tr>' for i in range(1, 900)
    )
    big = (
        "<!doctype html><html><head><meta charset='utf-8'><style>"
        "body{margin:0}.sheet{width:800px;padding:40px}</style></head>"
        "<body><div class='sheet'><h1>Ledger</h1><table>" + filler
        + "</table></div></body></html>"
    )
    assert len(big) > HTML_WARN_SIZE, len(big)

    cfg = load_config()
    cfg.llm.base_url = llm_base
    cfg.llm.timeout = 30
    cfg.renderer.url = renderer_url
    cfg.renderer.timeout = 30
    cfg.loop.max_rounds = 1

    mock_services.LLMHandler.plan_calls = 0
    mock_services.LLMHandler.bootstrap_html_override = big
    mock_services.LLMHandler.force_length_on_action = True
    mock_services.LLMHandler.last_action_html_len = None
    out = ROOT / "out" / "trunc_test"
    if out.exists():
        shutil.rmtree(out)
    try:
        summary = Pipeline(cfg, out).build(make_source_png(Path("tmp/source_fixture.png")))
    finally:
        mock_services.LLMHandler.bootstrap_html_override = None
        mock_services.LLMHandler.force_length_on_action = False

    # The whole document must reach ACTION -- no silent mid-document cut.
    seen = mock_services.LLMHandler.last_action_html_len
    assert seen == len(big), f"ACTION received {seen} of {len(big)} chars"
    ok(f"ACTION received the full {len(big)}-char document, untruncated")

    # A generation cut off at max_tokens must never be adopted.
    assert summary["rounds"][0]["decision"] == "rejected", summary["rounds"]
    assert "max_tokens" in summary["rounds"][0]["error"], summary["rounds"][0]["error"]
    ok("finish_reason=length rejected the round instead of adopting it")

    # Rejection must leave the current HTML untouched.
    assert (out / "clone.html").read_text() == big
    assert not (out / "rounds" / "r01" / "candidate.png").exists()
    ok("rejected round left clone.html at the previous document")




# --------------------------------------------------------------- 7. patch mode

def test_patch_mode() -> None:
    print("[7] patch mode guards")
    from utils import apply_edits

    doc = "<style>h1{font-size:28px}p{font-size:12px}</style><body><p>a</p><p>b</p></body>"

    out, applied = apply_edits(doc, [{"find": "font-size:28px", "replace": "font-size:31px"}])
    assert "font-size:31px" in out and "font-size:12px" in out, out
    assert len(applied) == 1
    ok("single unique edit applies and leaves the rest untouched")

    out2, _ = apply_edits(doc, [
        {"find": "h1{font-size:28px}", "replace": "h1{font-size:31px}"},
        {"find": "<p>a</p>", "replace": "<p>A</p>"},
    ])
    assert "31px" in out2 and "<p>A</p>" in out2 and "<p>b</p>" in out2
    ok("multiple edits apply in order")

    for label, edits in [
        ("missing 'find'", [{"find": "NOT_PRESENT", "replace": "x"}]),
        ("ambiguous 'find'", [{"find": "<p>", "replace": "<div>"}]),
        ("empty edit list", []),
        ("no-op edit", [{"find": "<p>a</p>", "replace": "<p>a</p>"}]),
        ("empty 'find'", [{"find": "", "replace": "x"}]),
        ("non-string 'replace'", [{"find": "<p>a</p>", "replace": 3}]),
        ("edit is not an object", ["nope"]),
    ]:
        try:
            apply_edits(doc, edits)
        except ValueError:
            pass
        else:
            raise AssertionError(f"{label} should have been rejected")
    ok("missing / ambiguous / empty / no-op / malformed edits all rejected")

    # An ambiguous find must not partially apply.
    try:
        apply_edits(doc, [{"find": "font-size:28px", "replace": "font-size:9px"},
                          {"find": "<p>", "replace": "<div>"}])
    except ValueError:
        pass
    ok("a failing later edit raises rather than leaving a half-applied patch")


def test_patch_artifacts() -> None:
    print("[8] patch artefacts and payload size")
    out = ROOT / "out" / "offline_test"

    modes = {r["round"]: r["mode"] for r in json.loads((out / "summary.json").read_text())["rounds"]}
    assert modes[1] == "patch" and modes[3] == "rewrite" and modes[4] == "patch", modes
    ok(f"summary.json records the action mode per round: {modes}")

    assert (out / "rounds" / "r01" / "patch.json").exists()
    patch = json.loads((out / "rounds" / "r01" / "patch.json").read_text())
    assert patch["edits"][0]["find"] == "font-size:28px", patch
    ok("patch.json saved alongside the round's other artefacts")

    # The point of patch mode: the response no longer scales with the document.
    raw = (out / "rounds" / "r01" / "action_raw.txt").read_text()
    doc = (out / "rounds" / "r01" / "candidate.html").read_text()
    assert len(raw) < len(doc) / 4, f"patch response {len(raw)} vs document {len(doc)}"
    rewrite_raw = (out / "rounds" / "r03" / "action_raw.txt").read_text()
    assert len(rewrite_raw) > len(raw) * 4, (len(rewrite_raw), len(raw))
    ok(f"patch response {len(raw)}B vs rewrite response {len(rewrite_raw)}B "
       f"for an {len(doc)}B document")





# ------------------------------------------------------ 9. operator in the loop

def test_operator(llm_base: str, renderer_url: str) -> None:
    print("[9] operator notes and interactive override")
    import shutil

    from pipeline import Pipeline, SkipRound

    cfg = load_config()
    cfg.llm.base_url = llm_base
    cfg.llm.timeout = 30
    cfg.renderer.url = renderer_url
    cfg.renderer.timeout = 30
    cfg.loop.max_rounds = 2

    # --- notes reach both PLAN and VERIFY prompts
    note = "표 정렬이 이 문서에서 가장 중요하다."
    seen: list[str] = []
    pipe = Pipeline(cfg, ROOT / "out" / "op_probe", notes=note)
    original = pipe.llm.chat

    def spy(messages, **kw):
        for part in messages[-1]["content"]:
            if part.get("type") == "text":
                seen.append(part["text"])
        return original(messages, **kw)

    pipe.llm.chat = spy
    src = make_source_png(Path("tmp/source_fixture.png"))
    mock_services.LLMHandler.plan_calls = 0
    png, _ = pipe.render(mock_services.GOOD_HTML.format(title=28, table_width="60%", rev=0))
    pipe.plan(src, png, [])
    pipe.verify({"goal": "g"}, src, png, png)
    assert sum(1 for t in seen if note in t) == 2, "notes must reach PLAN and VERIFY"
    assert any("Judge the images first" in t for t in seen)
    ok("operator notes injected into PLAN and VERIFY, with the images-first caveat")

    # --- an empty note changes nothing
    plain = Pipeline(cfg, ROOT / "out" / "op_probe", notes="   ")
    assert prompts.notes_block(plain.notes) == ""
    ok("blank notes add nothing to the prompts")

    # --- interactive: instruction reaches ACTION via the plan JSON
    pipe.interactive = True
    pipe._ask = lambda _p: "제목 크기부터 맞춰라"
    plan = pipe.review_plan({"scope": "local", "goal": "g"})
    assert plan["operator_instruction"] == "제목 크기부터 맞춰라", plan
    assert pipe.interventions == 1
    ok("operator instruction lands in plan.json, which ACTION receives verbatim")

    # --- interactive: skipping a round
    pipe._ask = lambda _p: "s"
    try:
        pipe.review_plan({"scope": "local"})
    except SkipRound:
        ok("operator can skip a round")
    else:
        raise AssertionError("skip was not honoured")

    # --- interactive: overriding VERIFY, keeping the model's own verdict
    pipe._ask = lambda _p: "revert"
    verdict = pipe.review_verify({"decision": "keep", "reason": "looks better"})
    assert verdict["decision"] == "revert", verdict
    assert verdict["model_decision"] == "keep", verdict
    assert verdict["operator_override"] == "revert", verdict
    ok("VERIFY override applied, model_decision preserved for honest evaluation")

    # --- an override equal to the model's decision is not counted as intervention
    before = pipe.interventions
    pipe._ask = lambda _p: "keep"
    pipe.review_verify({"decision": "keep"})
    assert pipe.interventions == before
    ok("agreeing with the model is not recorded as an intervention")

    # --- garbage input leaves the verdict alone
    pipe._ask = lambda _p: "asdf"
    v = pipe.review_verify({"decision": "done"})
    assert v["decision"] == "done" and "operator_override" not in v
    ok("unrecognised input leaves the model verdict untouched")

    # --- non-interactive pipelines never prompt
    quiet = Pipeline(cfg, ROOT / "out" / "op_probe")
    def boom(_p):
        raise AssertionError("must not prompt when interactive is off")
    quiet._ask = boom
    assert quiet.review_plan({"scope": "local"}) == {"scope": "local"}
    assert quiet.review_verify({"decision": "keep"})["decision"] == "keep"
    ok("non-interactive runs never block on input")

    # --- a full build records the intervention counts
    mock_services.LLMHandler.plan_calls = 0
    out = ROOT / "out" / "op_build"
    if out.exists():
        shutil.rmtree(out)
    run = Pipeline(cfg, out, notes=note, interactive=True)
    run._ask = lambda p: "revert" if "판정" in p else ""
    summary = run.build(src)
    assert summary["operator_notes"] == note
    assert summary["operator_interventions"] >= 1, summary
    assert summary["rounds"][0]["operator"] is True, summary["rounds"][0]
    assert json.loads((out / "rounds" / "r01" / "verify.json").read_text())["model_decision"] == "keep"
    ok(f"summary.json records {summary['operator_interventions']} intervention(s) "
       f"across {summary['operator_rounds']} round(s)")



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
    test_truncation(llm_base, renderer_url)
    test_patch_mode()
    test_patch_artifacts()
    test_operator(llm_base, renderer_url)
    print(f"\n{len(PASS)} checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
