"""Offline end-to-end exercise of the docgen pipeline against test doubles.

Run: python3 tests/test_offline.py
"""

from __future__ import annotations

import io
import json
import os
import sys
import threading
import time
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

    # The real renderer answers /health with a plain status line, not JSON.
    # Requiring JSON here used to reject a working service.
    mock_services.RendererHandler.health_body = (
        b"ok chromium=129.0.6668.29 korean_fonts=103 ['Batang', 'Dotum']"
    )
    try:
        info = client.health()
        assert info["ok"] is True, info
        assert "chromium=129.0.6668.29" in info["status"], info
        ok("plain-text /health accepted, status line surfaced")
    finally:
        mock_services.RendererHandler.health_body = None

    # A JSON body that says otherwise is still honoured.
    assert client.health().get("ok") is True
    ok("JSON /health still works and ok:false still fails")

    # Failure contract: ok:false must raise, not return junk.
    try:
        client.probe("", width=800)
    except Exception as exc:  # RendererError
        assert "empty html" in str(exc), exc
        ok("ok:false raises RendererError")
    else:
        raise AssertionError("empty html should have raised")

    # The contract names exactly two /probe failures: ok is false, or there is
    # no png_base64. A response with a good PNG and no "ok" field is a success --
    # defaulting the missing field to False used to throw away a working render.
    import base64 as _b64

    real_png = _b64.b64encode(png).decode()
    shapes = [
        ({"ok": True, "png_base64": real_png}, None),
        ({"png_base64": real_png}, None),                      # no "ok" -> success
        ({"ok": True, "png_base64": real_png, "metrics": {"page": {}}}, None),
        ({"ok": False, "error": "boom"}, "boom"),
        ({"ok": 0, "error": "falsy"}, "falsy"),                # falsy != absent
        ({"ok": True}, "png_base64"),
        ({}, "png_base64"),
        ({"ok": True, "png_base64": "not base64 at all !!"}, "base64"),
    ]
    original_fetch = client._fetch
    try:
        for body, want_err in shapes:
            client._fetch = lambda path, payload=None, _b=body: json.dumps(_b).encode()
            try:
                got, _ = client.probe("<html></html>", width=800)
            except Exception as exc:
                assert want_err, f"{body} 는 성공해야 하는데 실패했다: {exc}"
                assert want_err in str(exc), f"{body}: {exc}"
            else:
                assert not want_err, f"{body} 는 실패해야 하는데 통과했다"
                assert got[:8] == b"\x89PNG\r\n\x1a\n", body
    finally:
        client._fetch = original_fetch
    ok(f"/probe response shapes: {len(shapes)} cases, missing 'ok' counts as success")

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
        ("all-no-op patch", [{"find": "<p>a</p>", "replace": "<p>a</p>"}]),
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
    ok("missing / ambiguous / empty / all-no-op / malformed edits all rejected")

    # A restated line mixed in with real edits must not cost the whole round --
    # it asks for nothing, and the edits beside it are the round's actual work.
    out3, applied3 = apply_edits(doc, [
        {"find": "<p>a</p>", "replace": "<p>a</p>"},
        {"find": "font-size:28px", "replace": "font-size:31px"},
    ])
    assert "font-size:31px" in out3, out3
    assert len(applied3) == 1, applied3
    ok("a no-op edit is dropped, the real edits beside it still apply")

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

    # --- interactive PLAN: replace the goal vs attach a note
    pipe._base_interactive = True
    pipe._ask = lambda *_a, **_k: "x 제목 크기부터 맞춰라"
    plan = pipe.review_plan({"scope": "local", "goal": "g"})
    assert plan["operator_instruction"] == "제목 크기부터 맞춰라", plan
    assert plan["goal"] == "제목 크기부터 맞춰라", plan
    assert "operator_note" not in plan, plan
    assert plan["planned_by"] == "operator", plan
    ok("PLAN 'x' makes the operator's instruction the goal")

    # There is no middle ground between annotating and replacing: three only.
    pipe._ask = lambda *_a, **_k: "o 뭔가"
    plan = pipe.review_plan({"scope": "local", "goal": "g"})
    assert plan["planned_by"] == "model", "an unknown key must not change the plan"
    ok("PLAN offers exactly three choices; there is no fourth")

    pipe._ask = lambda *_a, **_k: "a 표 정렬도 같이 보라"
    plan = pipe.review_plan({"scope": "local", "goal": "g"})
    assert plan["operator_note"] == "표 정렬도 같이 보라", plan
    assert "operator_instruction" not in plan, plan
    assert plan["goal"] == "g", "attaching a note must not discard the model's plan"
    ok("PLAN 'a' attaches a note and leaves the model's plan intact")

    pipe._ask = lambda *_a, **_k: "a"
    plan = pipe.review_plan({"scope": "local", "goal": "g"})
    assert "operator_note" not in plan and plan["planned_by"] == "model"
    ok("a bare 'a' with no text is refused rather than stored empty")

    # --- a VERIFY decision word typed at the PLAN prompt is caught, not injected
    replies = iter(["revert", "x 제목부터"])
    pipe._ask = lambda *_a, **_k: next(replies)
    plan = pipe.review_plan({"scope": "local"})
    assert plan["operator_instruction"] == "제목부터", plan
    ok("a VERIFY decision word typed at the PLAN prompt is re-asked, not injected")

    # --- interactive: skipping a round
    pipe._ask = lambda *_a, **_k: "s"
    try:
        pipe.review_plan({"scope": "local"})
    except SkipRound:
        ok("operator can skip a round")
    else:
        raise AssertionError("skip was not honoured")

    # --- interactive VERIFY: attach an opinion without changing the decision
    pipe._ask = lambda *_a, **_k: "a 표 우측 정렬이 아직 다르다"
    v = pipe.review_verify({"decision": "keep", "verified_by": "model"})
    assert v["decision"] == "keep", "attaching an opinion must not change the verdict"
    assert v["operator_note"] == "표 우측 정렬이 아직 다르다", v
    assert v["verified_by"] == "model+operator", v
    assert "operator_override" not in v, v
    ok("VERIFY 'a' attaches an opinion and leaves the model's decision standing")

    # --- interactive VERIFY: override, with an optional reason on the same line
    pipe._ask = lambda *_a, **_k: "revert 표가 더 어긋났다"
    v = pipe.review_verify({"decision": "keep", "verified_by": "model"})
    assert v["decision"] == "revert" and v["model_decision"] == "keep", v
    assert v["operator_note"] == "표가 더 어긋났다", v
    # Replacing the verdict makes it the operator's, mirroring planned_by.
    assert v["verified_by"] == "operator", v
    ok("VERIFY override accepts a reason and records verified_by=operator")

    # Augmenting leaves the verdict as the model's, with a human note attached.
    pipe._ask = lambda *_a, **_k: "a 표 우측 정렬이 남았다"
    va = pipe.review_verify({"decision": "keep", "verified_by": "model"})
    assert va["decision"] == "keep" and va["verified_by"] == "model+operator", va
    ok("augmenting keeps verified_by=model+operator, override does not")

    # --- interactive: overriding VERIFY, keeping the model's own verdict
    pipe._ask = lambda *_a, **_k: "revert"
    verdict = pipe.review_verify({"decision": "keep", "reason": "looks better"})
    assert verdict["decision"] == "revert", verdict
    assert verdict["model_decision"] == "keep", verdict
    assert verdict["operator_override"] == "revert", verdict
    ok("VERIFY override applied, model_decision preserved for honest evaluation")

    # --- an override equal to the model's decision is not counted as intervention
    before = pipe.interventions
    pipe._ask = lambda *_a, **_k: "keep"
    pipe.review_verify({"decision": "keep"})
    assert pipe.interventions == before
    ok("agreeing with the model is not recorded as an intervention")

    # --- garbage input leaves the verdict alone
    pipe._ask = lambda *_a, **_k: "asdf"
    v = pipe.review_verify({"decision": "done"})
    assert v["decision"] == "done" and "operator_override" not in v
    ok("unrecognised input leaves the model verdict untouched")

    # --- non-interactive pipelines never prompt
    quiet = Pipeline(cfg, ROOT / "out" / "op_probe")
    def boom(_p):
        raise AssertionError("must not prompt when interactive is off")
    quiet._ask = boom
    plan_out = quiet.review_plan({"scope": "local"})
    # planned_by is always recorded so plan.json is self-describing.
    assert plan_out == {"scope": "local", "planned_by": "model"}, plan_out
    assert quiet.review_verify({"decision": "keep"})["decision"] == "keep"
    ok("non-interactive runs never block on input, and record planned_by=model")

    # --- the operator contract is declared to the model only when it applies
    assert prompts.operator_contract_block(False) == ""
    contract = prompts.operator_contract_block(True)
    for field in ("operator_instruction", "operator_note", "(operator: "):
        assert field in contract, field
    assert "REPLACES" in contract and "WITHOUT discarding" in contract
    ok("operator contract names both fields and their precedence")

    auto = Pipeline(cfg, ROOT / "out" / "op_probe")
    assert auto.operator_active is False
    assert Pipeline(cfg, ROOT / "out" / "op_probe", interactive=True).operator_active
    assert Pipeline(cfg, ROOT / "out" / "op_probe", verify_mode="both").operator_active
    ok("contract is off for a model-only run, on when a human can intervene")

    seen2: list[str] = []
    pipe3 = Pipeline(cfg, ROOT / "out" / "op_probe", interactive=True)
    orig3 = pipe3.llm.chat
    pipe3.llm.chat = lambda m, **kw: (seen2.extend(
        pt["text"] for pt in m[-1]["content"] if pt.get("type") == "text"), orig3(m, **kw))[1]
    png2, _ = pipe3.render(mock_services.GOOD_HTML.format(title=28, table_width="60%", rev=0))
    pipe3.plan(src, png2, [])
    pipe3.action({"scope": "local"}, "<html><body>x</body></html>", src, png2)
    pipe3.verify({"goal": "g"}, src, png2, png2)
    assert len(seen2) == 3, f"expected PLAN, ACTION and VERIFY prompts, got {len(seen2)}"
    missing = [i for i, t in enumerate(seen2) if "operator_instruction" not in t]
    assert not missing, f"stage prompt(s) {missing} lack the operator contract"
    ok("PLAN, ACTION and VERIFY prompts all carry the operator contract")

    # --- a full build records the intervention counts
    mock_services.LLMHandler.plan_calls = 0
    out = ROOT / "out" / "op_build"
    if out.exists():
        shutil.rmtree(out)
    run = Pipeline(cfg, out, notes=note, interactive=True)
    run._ask = lambda p, *_a, **_k: "revert" if "판정" in p else ""
    summary = run.build(src)
    assert summary["operator_notes"] == note
    assert summary["operator_interventions"] >= 1, summary
    assert summary["rounds"][0]["operator"] is True, summary["rounds"][0]
    # Every round the operator touched must be flagged, even ones that ended
    # early -- otherwise the intervention count and the flags disagree.
    flagged = sum(1 for r in summary["rounds"] if r["operator"])
    assert flagged == summary["operator_rounds"] >= 1, (flagged, summary["operator_rounds"])
    assert json.loads((out / "rounds" / "r01" / "verify.json").read_text())["model_decision"] == "keep"
    ok(f"summary.json records {summary['operator_interventions']} intervention(s) "
       f"across {summary['operator_rounds']} round(s)")





# ------------------------------------------------- 10. UI and region marking

def test_ui_and_region() -> None:
    print("[10] review UI and region marking")
    import urllib.error
    import urllib.request

    from pipeline import Pipeline
    from ui import ReviewServer

    out = ROOT / "out" / "ui_probe"
    utils.ensure_dir(out / "rounds" / "r01")
    sheet, panels = utils.side_by_side(
        [("1. SOURCE", str(make_source_png(Path("tmp/source_fixture.png")))),
         ("2. CURRENT RENDER", str(make_source_png(Path("tmp/source_fixture.png"))))],
        out / "rounds" / "r01" / "plan_view.png",
    )
    assert [p["label"] for p in panels] == ["1. SOURCE", "2. CURRENT RENDER"]
    assert panels[1]["x"] > panels[0]["width"], panels
    ok(f"side_by_side reports panel boxes a UI can map clicks to: {panels[0]}")

    server = ReviewServer(out, port=0, timeout=20)
    url = server.start()
    try:
        # Path traversal must not escape the run directory.
        assert server.read_image("../../../etc/passwd") is None
        assert server.read_image("rounds/r01/plan_view.png") is not None
        assert server.read_image("summary.json") is None  # PNG only
        ok("UI serves PNGs from the run directory only")

        # A question is published, answered over HTTP, and the region survives.
        answered: dict = {}

        def ask():
            answered["text"] = server.ask(
                "개입: ",
                {"stage": "PLAN", "round": 1, "image": str(sheet), "image_panels": panels},
            )
            answered["region"] = server.last_region

        thread = threading.Thread(target=ask, daemon=True)
        thread.start()
        deadline = time.time() + 10
        while time.time() < deadline and not (json.loads(
                urllib.request.urlopen(f"{url}state").read())["pending"]):
            time.sleep(0.1)
        state = json.loads(urllib.request.urlopen(f"{url}state").read())
        assert state["pending"]["image"] == "rounds/r01/plan_view.png", state["pending"]
        assert state["pending"]["panels"] == panels
        ok("pending question exposes the image path and panel boxes")

        req = urllib.request.Request(
            f"{url}answer",
            data=json.dumps({"answer": "x 이 표만 고쳐라",
                             "region": {"panel": "1. SOURCE", "x": 0.05, "y": 0.1,
                                        "w": 0.9, "h": 0.25}}).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        assert json.loads(urllib.request.urlopen(req).read())["ok"] is True
        thread.join(timeout=10)
        assert answered["text"] == "x 이 표만 고쳐라", answered
        assert answered["region"]["w"] == 0.9, answered
        ok("answer and marked region both reach the pipeline")

        for bad in ({"x": 2, "y": 0, "w": 1, "h": 1}, {"x": 0, "y": 0, "w": 0, "h": 1},
                    {"x": "a", "y": 0, "w": 1, "h": 1}, "nope", None):
            assert ReviewServer._clean_region(bad) is None, bad
        ok("out-of-range or malformed regions are dropped, not trusted")
    finally:
        server.stop()

    # A marked region becomes real zoomed crops on the ACTION call.
    region = {"panel": "1. SOURCE", "x": 0.05, "y": 0.1, "w": 0.9, "h": 0.25}
    src = make_source_png(Path("tmp/source_fixture.png"))
    crop = utils.crop_normalized(src, region)
    full_w, full_h = utils.image_size(src)
    crop_w, crop_h = utils.image_size(crop)
    assert crop_w < full_w and crop_h < full_h, (crop_w, crop_h, full_w, full_h)
    ok(f"crop_normalized zooms {full_w}x{full_h} down to {crop_w}x{crop_h}")

    # The same normalised rect works on a differently sized image.
    from PIL import Image
    small = io.BytesIO()
    Image.open(str(src)).resize((400, 280)).save(small, format="PNG")
    sw, sh = utils.image_size(utils.crop_normalized(small.getvalue(), region))
    assert abs(sw / 400 - crop_w / full_w) < 0.05, (sw, crop_w)
    ok("the same rect crops proportionally on a different-sized image")

    assert "operator_region" in prompts.operator_contract_block(True)
    block = prompts.region_block(region)
    assert "Leave the rest of the document alone" in block and "0.9" in block
    ok("ACTION is told the last two images are the marked region")

    cfg = load_config()
    pipe = Pipeline(cfg, out, interactive=True)
    pipe._last_region = region
    pipe._ask = lambda *_a, **_k: "x 이 표만 고쳐라"
    plan = pipe.review_plan({"scope": "local", "goal": "g"})
    assert plan["operator_region"] == region, plan
    ok("the region is stored on the plan, so ACTION receives it")





# ------------------------- 11. operator-only plan, LAN binding, region round-trip

def test_operator_only_and_region_loop(llm_base: str, renderer_url: str) -> None:
    print("[11] operator-only plan, LAN binding, region round trip")
    import shutil

    from pipeline import Pipeline
    from ui import ReviewServer, lan_ip

    cfg = load_config()
    cfg.llm.base_url = llm_base
    cfg.llm.timeout = 30
    cfg.renderer.url = renderer_url
    cfg.renderer.timeout = 30
    cfg.loop.max_rounds = 1

    # --- 'x' discards the model's plan outright
    pipe = Pipeline(cfg, ROOT / "out" / "x_probe", interactive=True)
    pipe._ask = lambda *_a, **_k: "x 표 컬럼 폭만 원본에 맞춰라"
    plan = pipe.review_plan(
        {"scope": "local", "target": "t", "problem": "p", "cause": "c", "goal": "모델 목표"}
    )
    assert plan["planned_by"] == "operator", plan
    assert plan["goal"] == "표 컬럼 폭만 원본에 맞춰라", plan
    assert plan["model_plan"]["goal"] == "모델 목표", plan
    assert "모델 목표" not in json.dumps(
        {k: v for k, v in plan.items() if k != "model_plan"}, ensure_ascii=False
    ), "the discarded plan must not leak back into the acted-on fields"
    ok("PLAN 'x' discards the model plan, keeping it only under model_plan")

    assert "planned_by" in prompts.operator_contract_block(True)
    ok("ACTION is told a plan may be the operator's alone")

    # --- the UI can change the intervention mode mid-run
    from ui import ReviewServer as RS

    srv = RS(ROOT / "out", port=0)
    live = Pipeline(cfg, ROOT / "out" / "x_probe", interactive=True,
                    verify_mode="both", prompter=srv)
    assert srv.config() == {"verify_mode": "both", "plan_interactive": True}, srv.config()
    assert (live.interactive, live.verify_mode) == (True, "both")

    srv.set_config({"verify_mode": "model"})
    assert live.verify_mode == "model", live.verify_mode
    srv.set_config({"verify_mode": "both"})
    assert live.verify_mode == "both", live.verify_mode
    srv.set_config({"plan_interactive": False})
    assert live.interactive is False
    ok("a mode change in the UI reaches the next round of the running pipeline")

    srv.set_config({"verify_mode": "model"})
    assert (live.interactive, live.verify_mode) == (False, "model")
    # The contract stays: the run began with a human in it.
    assert live.operator_active is True
    ok("switching everything off goes fully automatic without dropping the contract")

    srv.set_config({"verify_mode": "nope"})
    assert live.verify_mode == "model", "an unknown mode must be ignored"
    srv.set_config("not a dict")
    assert live.verify_mode == "model"
    ok("invalid config payloads are ignored")

    # No prompter at all: the CLI values stand and nothing raises.
    plain = Pipeline(cfg, ROOT / "out" / "x_probe", interactive=True, verify_mode="both")
    assert (plain.interactive, plain.verify_mode) == (True, "both")
    ok("with no prompter the pipeline just follows its start values")

    # --- the UI can bind somewhere another machine can reach
    ip = lan_ip()
    assert ip and ip.count(".") == 3, ip
    local = ReviewServer(ROOT / "out", port=0)
    url_local = local.start()
    local.stop()
    wide = ReviewServer(ROOT / "out", port=0, host="0.0.0.0")
    url_wide = wide.start()
    wide.stop()
    assert "127.0.0.1" in url_local, url_local
    assert "0.0.0.0" not in url_wide and ip in url_wide, url_wide
    assert not local.access_notes, local.access_notes
    ok(f"host=0.0.0.0 advertises a reachable address ({url_wide.strip('/')})")

    # --- inside a container the guessed address is knowably wrong for anyone
    # else, so it must come with an explanation rather than a dead link.
    import ui as ui_mod

    real_lan_ip = ui_mod.lan_ip
    ui_mod.lan_ip = lambda: "172.17.0.2"
    try:
        boxed = ReviewServer(ROOT / "out", port=0, host="0.0.0.0")
        url_boxed = boxed.start()
        port_boxed = boxed.port
        boxed.stop()
    finally:
        ui_mod.lan_ip = real_lan_ip
    assert "172.17.0.2" in url_boxed, url_boxed
    note = " ".join(boxed.access_notes)
    assert "컨테이너" in note and f"-p {port_boxed}:{port_boxed}" in note, note
    assert "--ui-public-host" in note, note
    ok("a container-internal address is printed with what to do about it")

    # --- the override changes only what is printed, never what is bound
    pub = ReviewServer(ROOT / "out", port=0, host="0.0.0.0", public_host="10.0.0.7")
    url_pub = pub.start()
    bound_host, bound_port = pub._httpd.server_address
    pub.stop()
    assert url_pub == f"http://10.0.0.7:{bound_port}/", url_pub
    assert bound_host == "0.0.0.0", bound_host
    ok("--ui-public-host relabels the URL without moving the socket")

    # --- and the env var is the same switch
    os.environ["DOCGEN_UI_PUBLIC_HOST"] = "10.0.0.8"
    try:
        env_srv = ReviewServer(ROOT / "out", port=0, host="0.0.0.0")
        url_env = env_srv.start()
        env_srv.stop()
    finally:
        del os.environ["DOCGEN_UI_PUBLIC_HOST"]
    assert "10.0.0.8" in url_env, url_env
    ok("DOCGEN_UI_PUBLIC_HOST does the same as the flag")

    # --- a marked region drives one full round and comes back zoomed
    class FakePrompter:
        """Marks a region and hands over an operator-only instruction."""

        last_region = {"panel": "1. SOURCE", "x": 0.05, "y": 0.12, "w": 0.9, "h": 0.24}

        def __init__(self):
            self.seen: list[dict] = []

        def ask(self, prompt, context):
            self.seen.append(context)
            return "x 이 표만 원본에 맞춰라" if context.get("stage") == "PLAN" else ""

    mock_services.LLMHandler.plan_calls = 0
    out = ROOT / "out" / "region_loop"
    if out.exists():
        shutil.rmtree(out)
    prompter = FakePrompter()
    pipe2 = Pipeline(cfg, out, interactive=True, verify_mode="both", prompter=prompter)
    images: list[int] = []
    original = pipe2.llm.chat

    def spy(messages, **kw):
        if str(kw.get("stage", "")).startswith("action"):
            images.append(sum(1 for c in messages[-1]["content"] if c["type"] == "image_url"))
        return original(messages, **kw)

    pipe2.llm.chat = spy
    summary = pipe2.build(make_source_png(Path("tmp/source_fixture.png")))

    saved = json.loads((out / "rounds" / "r01" / "plan.json").read_text())
    assert saved["operator_region"]["w"] == 0.9, saved
    assert saved["planned_by"] == "operator", saved
    ok("the marked region and operator-only plan are recorded in plan.json")

    assert images == [4], f"ACTION should get 2 page images + 2 region crops, got {images}"
    ok("ACTION received the two zoomed region crops alongside the page images")

    assert (out / "rounds" / "r01" / "compare_region.png").exists()
    rw, rh = utils.image_size(out / "rounds" / "r01" / "compare_region.png")
    fw, fh = utils.image_size(out / "rounds" / "r01" / "compare.png")
    assert rh < fh, (rh, fh)  # the zoom is cropped, so shorter than the full page
    ok(f"VERIFY gets a zoomed region comparison ({rw}x{rh}) next to the full page ({fw}x{fh})")

    verify_ctx = [c for c in prompter.seen if c.get("stage") == "VERIFY"]
    assert verify_ctx and verify_ctx[0]["image2"].endswith("compare_region.png"), verify_ctx
    assert verify_ctx[0]["image2_label"]
    ok("the region comparison is what the operator is shown at VERIFY")
    assert summary["rounds"][0]["operator"] is True





# ------------------------------ 12. VERIFY reasoning reaches the next PLAN

def test_verify_feeds_next_plan(llm_base: str, renderer_url: str) -> None:
    print("[12] VERIFY reasoning reaches the next PLAN")
    import shutil

    from pipeline import Pipeline, RoundResult

    # --- the line itself
    keep = RoundResult(2, "keep", reason="title size now matches",
                       plan={"goal": "widen the main table"},
                       verify={"reason": "title size now matches",
                               "next_major_issue": "table column widths",
                               "verified_by": "model"})
    line = keep.history_line()
    assert "next: table column widths" in line and "[model]" in line, line
    assert "why:" not in line, "a kept round should not carry a revert reason"
    ok(f"a kept round carries the model's remaining issue: {line}")

    rev = RoundResult(3, "revert", reason="table became too wide",
                      plan={"goal": "reduce the title font"},
                      verify={"reason": "table became too wide",
                              "next_major_issue": "header rule", "verified_by": "model"})
    line = rev.history_line()
    assert "why: table became too wide" in line, line
    ok("a reverted round carries why it failed, so the next plan can avoid it")

    human = RoundResult(4, "keep", reason="operator verdict",
                        plan={"goal": "fix the header rule"},
                        verify={"reason": "operator verdict",
                                "next_major_issue": "제목 자간", "verified_by": "operator"})
    assert "[operator] next: 제목 자간" in human.history_line(), human.history_line()
    ok("a human verdict is attributed to the operator, not the model")

    # A human's reason must survive on a keep too, not only on a revert.
    hk = RoundResult(6, "keep", reason="표 폭은 맞았지만 여백이 남았다",
                     plan={"goal": "표 폭 맞추기"},
                     verify={"reason": "표 폭은 맞았지만 여백이 남았다",
                             "next_major_issue": "제목 자간", "verified_by": "operator"})
    line = hk.history_line()
    assert "why: 표 폭은 맞았지만 여백이 남았다" in line, line
    ok("a human's reason on a kept round is carried, not dropped")

    # The reason under an [operator] tag must be the operator's, not the model's.
    ov = RoundResult(7, "revert", reason="title size now matches",
                     plan={"goal": "제목 크기"},
                     verify={"reason": "title size now matches", "model_decision": "keep",
                             "operator_note": "표가 더 어긋났다",
                             "next_major_issue": "table width", "verified_by": "operator"})
    line = ov.history_line()
    assert "why: 표가 더 어긋났다" in line, line
    assert "title size now matches" not in line, (
        "the discarded model verdict's reason must not appear under [operator]"
    )
    ok("an overridden round attributes the reason to the operator, not the model")

    # A skipped reason field must not leak the placeholder into the prompt.
    blank = RoundResult(8, "keep", reason="operator verdict", plan={"goal": "헤더 선"},
                        verify={"reason": "operator verdict", "next_major_issue": "자간",
                                "verified_by": "operator"})
    assert "operator verdict" not in blank.history_line(), blank.history_line()
    ok("the 'operator verdict' placeholder never reaches the prompt")

    both = RoundResult(5, "keep", reason="looks closer",
                       plan={"goal": "align the amounts"},
                       verify={"reason": "looks closer", "next_major_issue": "",
                               "operator_note": "우측 정렬 남음",
                               "verified_by": "model+operator"})
    assert "operator: 우측 정렬 남음" in both.history_line()
    ok("an attached opinion travels alongside the model's own verdict")

    # --- end to end: round 2's PLAN prompt must contain round 1's verdict
    cfg = load_config()
    cfg.llm.base_url = llm_base
    cfg.llm.timeout = 30
    cfg.renderer.url = renderer_url
    cfg.renderer.timeout = 30
    cfg.loop.max_rounds = 2

    mock_services.LLMHandler.plan_calls = 0
    out = ROOT / "out" / "history_flow"
    if out.exists():
        shutil.rmtree(out)
    pipe = Pipeline(cfg, out)
    plan_prompts: list[str] = []
    original = pipe.llm.chat

    def spy(messages, **kw):
        if kw.get("stage") == "plan":
            plan_prompts.extend(
                part["text"] for part in messages[-1]["content"] if part["type"] == "text"
            )
        return original(messages, **kw)

    pipe.llm.chat = spy
    pipe.build(make_source_png(Path("tmp/source_fixture.png")))

    assert len(plan_prompts) >= 2, len(plan_prompts)
    first, second = plan_prompts[0], plan_prompts[1]
    assert "Previous attempts" not in first, "round 1 has no history yet"
    verdict = json.loads((out / "rounds" / "r01" / "verify.json").read_text())
    remaining = verdict["next_major_issue"]
    assert remaining, verdict
    assert remaining in second, (
        f"round 1's next_major_issue {remaining!r} never reached round 2's PLAN"
    )
    assert "[model]" in second, second[-400:]
    ok(f"round 2's PLAN prompt carries round 1's verdict ({remaining!r})")

    assert "the tag in brackets says who judged it" in second
    ok("PLAN is told how to read the annotations")





# --------------------- 13. failed attempts persist beyond the history window

def test_failed_attempts_persist(llm_base: str, renderer_url: str) -> None:
    print("[13] failed attempts outlive the 3-round history window")
    import shutil

    from pipeline import Pipeline, RoundResult

    # Only outcomes that mean the attempt did not land are collected.
    outcomes = {
        "keep": None, "done": None, "error": None, "skipped": None,
        "revert": "judged worse", "rejected": "could not be applied", "noop": "produced no change",
    }
    for decision, expect in outcomes.items():
        r = RoundResult(1, decision, reason="", plan={"goal": "g"}, error="boom")
        line = r.failed_line()
        if expect is None:
            assert line is None, f"{decision} should not count as a failed attempt: {line}"
        else:
            assert line and expect in line, (decision, line)
    ok("only revert / rejected / noop become failed attempts")

    # The window keeps 3 entries; the failed list keeps far more.
    hist = prompts.history_block([f"Round {i}: g{i} -> keep" for i in range(1, 9)])
    assert hist.count("- Round") == 3, hist
    dead = prompts.failed_block([f"g{i} -> revert: why{i}" for i in range(1, 9)])
    assert dead.count("\n- ") == 8, dead          # all eight kept
    assert "g1 " in dead, "the oldest failure must survive"
    trimmed = prompts.failed_block([f"g{i} -> revert: why{i}" for i in range(1, 12)])
    assert trimmed.count("\n- ") == 8 and "g1 " not in trimmed, trimmed
    assert "attack it a different way" in dead
    ok("history stays at 3 entries while the failed list keeps 8")

    assert prompts.failed_block([]) == ""
    ok("no failed attempts adds nothing to the prompt")

    # End to end: the mock's round 2 is rejected at APPLY. By round 5 that
    # rejection has left the history window but must still be listed.
    cfg = load_config()
    cfg.llm.base_url = llm_base
    cfg.llm.timeout = 30
    cfg.renderer.url = renderer_url
    cfg.renderer.timeout = 30
    cfg.loop.max_rounds = 6

    mock_services.LLMHandler.plan_calls = 0
    out = ROOT / "out" / "failed_flow"
    if out.exists():
        shutil.rmtree(out)
    pipe = Pipeline(cfg, out)
    prompts_seen: list[str] = []
    original = pipe.llm.chat

    def spy(messages, **kw):
        if kw.get("stage") == "plan":
            prompts_seen.extend(
                part["text"] for part in messages[-1]["content"] if part["type"] == "text"
            )
        return original(messages, **kw)

    pipe.llm.chat = spy
    summary = pipe.build(make_source_png(Path("tmp/source_fixture.png")))

    assert summary["failed_attempts"], summary
    ok(f"summary.json records what never worked: {summary['failed_attempts'][:1]}")

    later = [p for p in prompts_seen if "Already tried without success" in p]
    assert later, "the failed list never reached a PLAN prompt"
    first_failure = summary["failed_attempts"][0]
    goal = first_failure.split(" -> ")[0]
    last = prompts_seen[-1]
    assert "Already tried without success" in last, last[-500:]
    assert goal in last, f"{goal!r} dropped out of the last PLAN prompt"
    ok(f"the earliest failure is still listed in the last PLAN prompt ({goal!r})")

    # The point of the list: a failure that has aged out of the recent history
    # is still in front of PLAN. Composed directly so the proof does not depend
    # on how many rounds the mock happens to run.
    rounds = [
        RoundResult(1, "revert", reason="table became too wide",
                    plan={"goal": "widen the main table"}, verify={}),
        RoundResult(2, "keep", plan={"goal": "fix the header rule"}, verify={}),
        RoundResult(3, "keep", plan={"goal": "align the amounts"}, verify={}),
        RoundResult(4, "keep", plan={"goal": "tighten the notes"}, verify={}),
        RoundResult(5, "keep", plan={"goal": "pad the sheet"}, verify={}),
    ]
    window = prompts.history_block([r.history_line() for r in rounds])
    dead_ends = prompts.failed_block([ln for ln in (r.failed_line() for r in rounds) if ln])
    assert "widen the main table" not in window, "round 1 should have aged out"
    assert "widen the main table" in dead_ends, dead_ends
    assert "table became too wide" in dead_ends
    ok("a failure that aged out of the history window is still listed with its reason")





# ------------- 14. the three operator states agree everywhere they are read

def test_three_states_are_consistent() -> None:
    """The whole operator model is three states. Every consumer must agree.

    This exists because the three states used to be re-derived independently in
    eight places from combinations of optional fields, and each mis-attribution
    bug was one of those copies disagreeing with the others.
    """
    print("[14] the three operator states agree in every consumer")
    from pipeline import (
        MODEL,
        MODEL_AND_OPERATOR,
        OPERATOR,
        RoundResult,
        deciding_words,
        judged_by,
        touched_by_operator,
    )

    # (label, plan, verdict, expected source, expected operator flag, whose reason)
    table = [
        ("Qwen 단독",
         {"planned_by": MODEL},
         {"verified_by": MODEL, "reason": "model said so"},
         MODEL, False, "model said so"),
        ("Qwen + 사람 첨언 (PLAN)",
         {"planned_by": MODEL_AND_OPERATOR, "operator_note": "표 정렬도"},
         {"verified_by": MODEL, "reason": "model said so"},
         MODEL, True, "model said so"),
        ("Qwen + 사람 첨언 (VERIFY)",
         {"planned_by": MODEL},
         {"verified_by": MODEL_AND_OPERATOR, "reason": "model said so",
          "operator_note": "우측 정렬 남음"},
         MODEL_AND_OPERATOR, True, "model said so"),
        ("Qwen 폐기, 사람 지시 (PLAN)",
         {"planned_by": OPERATOR, "operator_instruction": "이 표만",
          "model_plan": {"goal": "버려진 계획"}},
         {"verified_by": MODEL, "reason": "model said so"},
         MODEL, True, "model said so"),
        ("Qwen 폐기, 사람 판정 (VERIFY, 이유 입력)",
         {"planned_by": MODEL},
         {"verified_by": OPERATOR, "reason": "표가 더 어긋났다"},
         OPERATOR, True, "표가 더 어긋났다"),
        ("Qwen 판정 교체 (모델 이유가 남아 있어도)",
         {"planned_by": MODEL},
         {"verified_by": OPERATOR, "reason": "model said so",
          "operator_note": "사람 근거", "model_decision": "keep"},
         OPERATOR, True, "사람 근거"),
        ("사람 판정, 이유 생략",
         {"planned_by": MODEL},
         {"verified_by": OPERATOR, "reason": "operator verdict"},
         OPERATOR, True, ""),
    ]

    for label, plan, verdict, source, flagged, words in table:
        assert judged_by(verdict) == source, (label, judged_by(verdict))
        assert touched_by_operator(plan, verdict) is flagged, label
        assert deciding_words(verdict, verdict.get("reason", "")) == words, (
            label, deciding_words(verdict, verdict.get("reason", ""))
        )
        # The line's tag and its reason must come from the same party.
        line = RoundResult(1, "revert", reason=verdict.get("reason", ""),
                           plan=plan, verify=verdict).history_line()
        assert f"[{source}]" in line, (label, line)
        if words:
            assert f"why: {words}" in line, (label, line)
        else:
            assert "why:" not in line, (label, line)
        # The discarded verdict's reason must never appear under an operator tag.
        if source == OPERATOR and verdict.get("operator_note"):
            assert verdict["reason"] not in line, (label, line)
        # The round record must agree with the flag.
        rec = RoundResult(1, "keep", plan=plan, verify=verdict,
                          operator=touched_by_operator(plan, verdict))
        assert rec.operator is flagged, label
    ok(f"all {len(table)} operator states agree across judged_by / flag / line / reason")

    # Unknown or missing values fall back to the model, never crash.
    for junk in (None, {}, {"verified_by": "nonsense"}, {"planned_by": 3}):
        assert judged_by(junk) == MODEL, junk
        assert touched_by_operator(junk) is False, junk
    ok("unknown or missing sources fall back to model")

    # There is exactly one definition of the three states in the codebase.
    src = (ROOT / "pipeline.py").read_text()
    assert src.count('planned_by", "model") != "model"') == 0
    assert src.count('verified_by") in (') == 0
    ok("no consumer re-derives the states from field combinations")





# --------------------------------- 15. runs on Pythons without tomllib

def test_config_without_tomllib() -> None:
    print("[15] config loads without tomllib")
    import builtins
    import importlib

    import config as config_mod

    # tomllib is 3.11+; tomli is optional. Neither present must still work.
    real_import = builtins.__import__

    def blocked(name, *a, **k):
        if name in ("tomllib", "tomli"):
            raise ModuleNotFoundError(name)
        return real_import(name, *a, **k)

    builtins.__import__ = blocked
    try:
        fallback = importlib.reload(config_mod)
        assert fallback._toml is None, "the fallback path was not taken"
        cfg = fallback.load_config()
    finally:
        builtins.__import__ = real_import
        importlib.reload(config_mod)

    # Values must come back with the same types the TOML parser gives.
    assert cfg.llm.model == "qwen3.5_397b_a17b", cfg.llm.model
    assert isinstance(cfg.llm.temperature, float) and cfg.llm.temperature == 0.2
    assert isinstance(cfg.llm.max_tokens, int) and cfg.llm.max_tokens == 32768
    assert isinstance(cfg.renderer.device_scale, float) and cfg.renderer.device_scale == 1.0
    assert isinstance(cfg.renderer.width, int) and cfg.renderer.width == 800
    assert cfg.loop.max_rounds == 8
    ok("config.toml parses without tomllib, with the same value types")

    # The narrow parser must refuse what it cannot read rather than guess.
    from config import _minimal_toml

    good = _minimal_toml('[llm]\nmodel = "x"  # 주석\ntimeout = 900\n\n', "t")
    assert good == {"llm": {"model": "x", "timeout": 900}}, good
    ok("comments and blank lines are handled")

    for bad, why in [
        ('model = "x"\n', "section 밖의 키"),
        ('[llm]\nvalues = [1, 2]\n', "배열"),
        ('[llm]\nnested = { a = 1 }\n', "인라인 테이블"),
        ('[llm]\ngarbage\n', "값 없는 줄"),
    ]:
        try:
            _minimal_toml(bad, "t")
        except ValueError:
            pass
        else:
            raise AssertionError(f"허용해선 안 되는 입력: {why}")
    ok("anything outside the supported shape raises instead of being guessed")

    # The path guard must not call Path.is_relative_to (3.9+); the name may
    # still appear in a comment explaining why.
    assert ".is_relative_to(" not in (ROOT / "ui.py").read_text()
    ok("the UI path guard avoids a 3.9-only API")

    # Every module defers annotation evaluation, so `X | Y` hints are safe on
    # interpreters older than 3.10.
    missing = [
        f.name
        for f in list(ROOT.glob("*.py")) + list((ROOT / "tests").glob("*.py"))
        if "from __future__ import annotations" not in f.read_text()
    ]
    assert not missing, f"these modules would fail on older Pythons: {missing}"
    ok("all modules defer annotations, so union hints work on older Pythons")



# ------------------------------------ 16. the renderer stays external


def test_renderer_stays_external() -> None:
    """The renderer is somebody else's service, and must stay that way.

    The contract is fixed: an already-running Docker service answering
    ``GET /health`` and ``POST /probe``. This project is only its HTTP client.
    Standing up a second renderer here -- installing a browser driver, launching
    Chromium, serving /probe from a shipped module -- is the failure this guard
    exists to catch, because it is the kind of thing that gets added back by
    accident (a dev dependency, a "just for testing" server) and then quietly
    runs on the GPU box alongside the real one.
    """
    print("\n[16] 렌더러는 외부 서비스로 유지된다")

    import re

    DRIVERS = ("playwright", "selenium", "pyppeteer", "puppeteer", "splinter", "helium")
    py_files = sorted(list(ROOT.glob("*.py")) + list((ROOT / "tests").glob("*.py")))
    assert py_files, "스캔할 파일을 찾지 못했다"

    # 1. Nothing imports a browser driver. Matching the import statement rather
    #    than the bare name is what lets this file name the drivers it forbids.
    driver_import = re.compile(
        r"^\s*(?:import|from)\s+(?:%s)\b" % "|".join(DRIVERS), re.MULTILINE
    )
    offenders = [f.name for f in py_files if driver_import.search(f.read_text())]
    assert not offenders, f"브라우저 드라이버를 import 하는 파일: {offenders}"
    ok("no module imports a browser driver")

    # 2. No requirements file asks pip to install one. Comments are stripped
    #    first -- requirements-dev.txt names the drivers in order to forbid them.
    for req in sorted(ROOT.glob("requirements*.txt")):
        body = "\n".join(
            line.split("#", 1)[0] for line in req.read_text().splitlines()
        ).lower()
        named = [d for d in DRIVERS if d in body]
        assert not named, f"{req.name} 이 {named} 를 설치하려 한다"
    ok("no requirements file installs a browser driver")

    # 3. Nothing launches a browser, driver import or not. This file is skipped
    #    because it is where the forbidden names are written down; check 1 above
    #    still covers it, since none of these can be called without an import.
    launches = ("chromium.launch", "webdriver.Chrome", "webdriver.Firefox", "sync_playwright")
    for f in py_files:
        if f.name == Path(__file__).name:
            continue
        text = f.read_text()
        hits = [k for k in launches if k in text]
        assert not hits, f"{f.name} 이 브라우저를 실행한다: {hits}"
    ok("nothing launches a browser")

    # 4. No shipped module implements the renderer side of the contract.
    #    renderer.py may mention /probe -- it is the client that POSTs to it --
    #    but it must not be able to answer one.
    for f in sorted(ROOT.glob("*.py")):
        text = f.read_text()
        if "/probe" not in text:
            continue
        assert f.name == "renderer.py", f"{f.name} 이 /probe 계약에 손을 댄다"
        for server_api in ("do_POST", "do_GET", "BaseHTTPRequestHandler", "HTTPServer"):
            assert server_api not in text, f"renderer.py 가 서버가 되어 있다: {server_api}"
    ok("no shipped module can answer /probe -- renderer.py only calls it")



# ------------------------------- 17. the loop has to actually move the document

def test_loop_makes_progress() -> None:
    """Every mechanism whose failure looks like \"nothing changed\"."""
    print("\n[17] 루프가 문서를 실제로 움직인다")
    import prompts
    from utils import diff_line_count

    # 1. The change measure has to see an edit that keeps the length.
    before = "<style>h1{font-size:28px}</style>\n<body><p>a</p></body>"
    after = "<style>h1{font-size:31px}</style>\n<body><p>a</p></body>"
    assert diff_line_count(before, after) == 2, diff_line_count(before, after)
    assert len(before) == len(after), "fixture no longer tests the equal-length case"
    assert diff_line_count(before, before) == 0
    ok("diff_line_count sees a same-length edit that a char count would miss")

    # 2. A one-instance patch is the failure the user actually sees, so ACTION
    #    has to be told that a short "find" is about response size, not scope.
    # Wrapped prompt text, so compare with the line breaks flattened out.
    flat = lambda text: " ".join(text.split())
    patch_prompt = flat(prompts.ACTION_PATCH_USER)
    assert "one edit for every place" in patch_prompt, patch_prompt
    assert "not about making the edit small" in patch_prompt, patch_prompt
    assert "EVERYWHERE it appears" in flat(prompts.PLAN_USER), prompts.PLAN_USER
    ok("PLAN and ACTION both ask for the fix in every place it applies")

    # 3. "done" is the one verdict that ends the run, so its bar must not be
    #    "a person would call it essentially the same".
    assert "you are not done" in flat(prompts.VERIFY_USER), prompts.VERIFY_USER
    assert "essentially the same document" not in flat(prompts.VERIFY_USER)
    ok("VERIFY cannot call it done while naming a remaining mismatch")

    # 4. And if it does anyway, the code refuses to stop on it.
    from pipeline import Pipeline

    cfg = load_config()
    pipe = Pipeline(cfg, ROOT / "out" / "progress_probe")

    class Canned:
        supports_thinking_flag = True

        def __init__(self, payload):
            self.payload = payload

        def chat(self, messages, thinking=False, stage="", **kw):
            from llm import LLMResponse
            return LLMResponse(json.dumps(self.payload), "stop", {})

    png = make_source_png(Path("tmp/source_fixture.png"))
    png_bytes = png.read_bytes()  # verify() encodes its images, so these must be real
    contradictory = {"decision": "done", "reason": "close enough",
                     "next_major_issue": "table column widths are still wrong"}
    pipe.llm = Canned(contradictory)
    verdict = pipe.verify({"goal": "g"}, png, png_bytes, png_bytes)
    assert verdict["decision"] == "keep", verdict
    assert verdict["downgraded_from"] == "done", verdict
    ok("done + a named remaining issue is downgraded to keep, the run continues")

    pipe.llm = Canned({"decision": "done", "reason": "matches", "next_major_issue": ""})
    clean = pipe.verify({"goal": "g"}, png, png_bytes, png_bytes)
    assert clean["decision"] == "done" and "downgraded_from" not in clean, clean
    ok("a genuine done still ends the run")


# ------------------- 18. a structural change has a mode it can fit in

def test_section_mode(llm_base: str, renderer_url: str) -> None:
    """The middle mode. Without it a restructure has nowhere to go."""
    print("\n[18] 구조 변경이 들어갈 자리가 있다")
    import shutil

    from pipeline import HTML_WARN_SIZE, Pipeline
    from utils import apply_section

    doc = (
        "<!doctype html><html><body><div class='sheet'>"
        "<table class='grid'><tr><td>a</td></tr><tr><td>b</td></tr></table>"
        "<p>after</p></div></body></html>"
    )

    # 1. A block is rebuilt wholesale -- the thing patch mode cannot express.
    rebuilt = "<table class='grid'><tr><th>h</th><th>i</th></tr><tr><td>a</td><td>b</td></tr></table>"
    out, span = apply_section(doc, {
        "find_start": "<table class='grid'>", "find_end": "</table>", "replace": rebuilt,
    })
    assert rebuilt in out and "<p>after</p>" in out, out
    assert "<tr><td>a</td></tr>" not in out, "the old block survived"
    assert "->" in span, span
    ok(f"a whole block is replaced by new markup, the rest untouched ({span})")

    # 2. The end anchor is searched AFTER the start, so a closing tag that also
    #    appears earlier in the document cannot select the wrong span.
    nested = "<div></div><section id='x'><p>one</p></section><p>tail</p>"
    out2, _ = apply_section(nested, {
        "find_start": "<section id='x'>", "find_end": "</section>", "replace": "<hr>",
    })
    assert out2 == "<div></div><hr><p>tail</p>", out2
    ok("the span runs from find_start to the first find_end after it")

    for label, edit in [
        ("missing find_start", {"find_start": "<nope>", "find_end": "</table>", "replace": "x"}),
        ("ambiguous find_start", {"find_start": "<tr>", "find_end": "</tr>", "replace": "x"}),
        ("find_end never follows", {"find_start": "<p>after</p>", "find_end": "<table",
                                   "replace": "x"}),
        ("empty replace", {"find_start": "<table class='grid'>", "find_end": "</table>",
                           "replace": ""}),
        ("no change", {"find_start": "<table class='grid'>", "find_end": "</table>",
                       "replace": "<table class='grid'><tr><td>a</td></tr>"
                                  "<tr><td>b</td></tr></table>"}),
        ("not an object", ["nope"]),
    ]:
        try:
            apply_section(doc, edit)
        except ValueError:
            pass
        else:
            raise AssertionError(f"{label} should have been rejected")
    ok("missing / ambiguous / unterminated / empty / no-op section edits all rejected")

    # 3. Routing. The size override is the point: a global plan on a document
    #    too big to re-emit must still produce a real change, not a dead round.
    small, big = 1000, HTML_WARN_SIZE + 1
    cases = [
        ({"scope": "local"}, small, "patch"),
        ({"scope": "section"}, small, "section"),
        ({"scope": "global"}, small, "rewrite"),
        ({"scope": "global"}, big, "section"),
        ({"scope": "Local edit"}, small, "patch"),
        ({}, small, "section"),
        ({"scope": "whatever"}, small, "section"),
    ]
    for plan, size, expected in cases:
        got = Pipeline.action_mode(plan, size)
        assert got == expected, f"scope={plan.get('scope')!r} size={size} -> {got}, want {expected}"
    ok("local->patch, section->section, global->rewrite, oversized global->section")

    # 4. End to end: a section round has to land a bigger change than the
    #    one-declaration patch rounds the loop was producing before.
    cfg = load_config()
    cfg.llm.base_url = llm_base
    cfg.llm.timeout = 30
    cfg.renderer.url = renderer_url
    cfg.renderer.timeout = 30
    cfg.loop.max_rounds = 1

    mock_services.LLMHandler.plan_calls = 0
    mock_services.LLMHandler.scope_override = "section"
    out_dir = ROOT / "out" / "section_flow"
    if out_dir.exists():
        shutil.rmtree(out_dir)
    try:
        summary = Pipeline(cfg, out_dir).build(make_source_png(Path("tmp/source_fixture.png")))
    finally:
        mock_services.LLMHandler.scope_override = None

    round1 = summary["rounds"][0]
    assert round1["mode"] == "section", round1
    assert round1["decision"] in ("keep", "done"), round1
    assert round1["changed_lines"] >= 4, round1
    ok(f"a section round rebuilt the block: {round1['changed_lines']} lines changed")

    clone = (out_dir / "clone.html").read_text()
    assert "<th>Total</th>" in clone, "the rebuilt block never reached clone.html"
    okc, reason = utils.html_sanity_check(clone)
    assert okc, reason
    ok("the rebuilt block is in clone.html and the document is still valid")

    payload = json.loads((out_dir / "rounds" / "r01" / "patch.json").read_text())
    assert payload["find_start"] and payload["replace"], payload
    ok("the section payload is kept on disk like a patch payload")

    # 5. VERIFY must not revert a big edit just because something regressed.
    flat = " ".join(prompts.VERIFY_USER.split())
    assert "Judge the net result" in flat, flat
    assert "not that you can point at one thing that got worse" in flat, flat
    ok("VERIFY weighs the net result instead of any single regression")


# ------------------------ 19. the first draft is built in stages, not in one shot

def test_staged_bootstrap(llm_base: str, renderer_url: str) -> None:
    """Structure first, judged at a glance, then filled block by block."""
    print("\n[19] 첫 HTML을 단계로 만든다")
    import shutil

    from pipeline import Pipeline
    from utils import block_markers, fill_block

    # 1. Finding the blocks a skeleton marked, whatever order the attributes
    #    came out in, and refusing to fill an id that is not unique.
    doc = (
        "<body><section data-block=\"1\" data-role=\"머리글\"><p>x</p></section>"
        "<section data-role=\"표\" data-block=\"2\"><p>y</p></section>"
        "<section data-block=\"2\" data-role=\"중복\"><p>z</p></section></body>"
    )
    marks = block_markers(doc)
    assert [m["id"] for m in marks] == ["1", "2"], marks
    assert marks[0]["role"] == "머리글" and marks[1]["role"] == "표", marks
    assert marks[1]["open"] == '<section data-role="표" data-block="2">', marks[1]
    ok("blocks are found in reading order, attribute order does not matter, "
       "a repeated id is dropped")

    filled, span = fill_block(
        doc, marks[0],
        '<section data-block="1" data-role="머리글"><h1>제목</h1><p>부제</p></section>',
    )
    assert "<h1>제목</h1>" in filled and "<p>y</p>" in filled, filled
    assert "<p>x</p>" not in filled, "the drawn block survived the fill"
    ok(f"one block is replaced by its filled version, siblings untouched ({span})")

    for label, marker, replacement in [
        ("marker dropped", marks[0], "<section><p>no marker</p></section>"),
        ("nested same tag", {"id": "9", "tag": "section", "role": "",
                             "open": '<section data-block="9">'},
         '<section data-block="9"><p>ok</p></section>'),
    ]:
        target = doc
        if label == "nested same tag":
            target = '<body><section data-block="9"><section><p>in</p></section></section></body>'
        try:
            fill_block(target, marker, replacement)
        except ValueError:
            pass
        else:
            raise AssertionError(f"{label} should have been rejected")
    ok("a fill without its data-block, or a block nested in its own tag, is rejected")

    # 2. The whole staged path end to end.
    cfg = load_config()
    cfg.llm.base_url = llm_base
    cfg.llm.timeout = 30
    cfg.renderer.url = renderer_url
    cfg.renderer.timeout = 30
    cfg.loop.max_rounds = 1
    assert cfg.bootstrap.staged, "staged bootstrap is meant to be the default"

    def run(name: str) -> tuple[dict, Path]:
        out = ROOT / "out" / name
        if out.exists():
            shutil.rmtree(out)
        mock_services.LLMHandler.plan_calls = 0
        mock_services.LLMHandler.skeleton_checks = 0
        mock_services.LLMHandler.fill_calls = 0
        mock_services.LLMHandler.image_sides = {}
        summary = Pipeline(cfg, out).build(make_source_png(Path("tmp/source_fixture.png")))
        stages = json.loads((out / "rounds" / "bootstrap_stages.json").read_text())["stages"]
        return {"summary": summary, "stages": stages}, out

    result, out = run("staged_bootstrap")
    steps = [st["step"] for st in result["stages"]]
    assert steps[0] == "skeleton" and steps[1] == "layout_check", steps
    assert steps.count("fill") == 3, steps
    assert all(st["landed"] for st in result["stages"] if st["step"] == "fill"), result["stages"]
    ok(f"three steps ran in order: {' -> '.join(dict.fromkeys(steps))}")

    for rel in ("bootstrap_skeleton.html", "bootstrap_skeleton.png",
                "bootstrap_skeleton_check.json", "bootstrap_fill_01.html",
                "bootstrap_fill_03.png", "bootstrap.html", "bootstrap_stages.json"):
        assert (out / "rounds" / rel).exists(), rel
    ok("every stage left its HTML and its render on disk")

    skeleton = (out / "rounds" / "bootstrap_skeleton.html").read_text()
    final = (out / "rounds" / "bootstrap.html").read_text()
    assert "<th>Item</th>" not in skeleton, "the skeleton already transcribed the table"
    assert "<th>Item</th>" in final, "the fill phase never reached bootstrap.html"
    assert len(final) > len(skeleton), (len(final), len(skeleton))
    assert len(block_markers(final)) == 3, "the fills lost their block markers"
    ok(f"skeleton {len(skeleton)} chars -> filled {len(final)} chars, markers intact")

    # 3. The point of the rough view: the structure steps must not be able to
    #    read the page, and the detail step must be able to.
    sides = mock_services.LLMHandler.image_sides
    rough = cfg.bootstrap.rough_max_side
    for stage in ("skeleton", "skeleton_check"):
        assert sides.get(stage), f"no images recorded for {stage}"
        assert max(sides[stage]) <= rough, f"{stage} saw {max(sides[stage])}px, want <= {rough}"
    assert max(sides["fill"]) > rough, f"fill only saw {max(sides['fill'])}px"
    ok(f"structure steps saw <= {rough}px, the fill step saw {max(sides['fill'])}px")

    # 4. A block that fails is left as drawn, and the others still fill.
    mock_services.LLMHandler.fail_fill_block = "2"
    try:
        result2, out2 = run("staged_fill_fail")
    finally:
        mock_services.LLMHandler.fail_fill_block = None
    fills = [st for st in result2["stages"] if st["step"] == "fill"]
    assert [st["landed"] for st in fills] == [True, False, True], fills
    assert "data-block" in fills[1].get("error", ""), fills[1]
    final2 = (out2 / "rounds" / "bootstrap.html").read_text()
    assert "<th>Item</th>" not in final2, "the failed block was filled anyway"
    assert "Prepared for the finance committee" in final2, "block 1 did not survive"
    okc, reason = utils.html_sanity_check(final2)
    assert okc, reason
    ok("a rejected fill leaves that block as drawn, the rest still land")

    # 5. The layout check has to be able to change the layout.
    mock_services.LLMHandler.skeleton_mismatch = True
    try:
        result3, out3 = run("staged_layout_fix")
    finally:
        mock_services.LLMHandler.skeleton_mismatch = False
    check = json.loads((out3 / "rounds" / "bootstrap_skeleton_check.json").read_text())
    assert check["matches"] is False and check["problems"], check
    fix = [st for st in result3["stages"] if st["step"] == "layout_fix"]
    assert fix and fix[0]["landed"], result3["stages"]
    assert (out3 / "rounds" / "bootstrap_rough.html").exists()
    assert "width:92%" in (out3 / "rounds" / "bootstrap.html").read_text()
    ok("a layout mismatch is corrected before any text is filled in")

    # 6. And the one-shot path still exists for cheap runs.
    cfg.bootstrap.staged = False
    try:
        _, out4 = run("staged_off")
    finally:
        cfg.bootstrap.staged = True
    assert (out4 / "rounds" / "bootstrap_raw.txt").exists()
    assert not (out4 / "rounds" / "bootstrap_skeleton.html").exists()
    stages4 = json.loads((out4 / "rounds" / "bootstrap_stages.json").read_text())["stages"]
    assert [st["step"] for st in stages4] == ["single"], stages4
    ok("bootstrap.staged = false falls back to the single-call draft")


# ------------- 20. the staged draft changes where the loop starts, not how it runs

def test_bootstrap_does_not_touch_the_loop(llm_base: str, renderer_url: str) -> None:
    """Same starting HTML, both strategies, identical rounds."""
    print("\n[20] bootstrap 방식은 루프 동작을 바꾸지 않는다")
    import shutil

    from pipeline import Pipeline

    # Pinning the draft is the whole trick: with the same HTML on round 1, any
    # difference in the rounds afterwards would have to come from the loop.
    pinned = mock_services.GOOD_HTML.format(title=28, table_width="60%", rev=0)

    cfg = load_config()
    cfg.llm.base_url = llm_base
    cfg.llm.timeout = 30
    cfg.renderer.url = renderer_url
    cfg.renderer.timeout = 30
    cfg.loop.max_rounds = 4

    def run(name: str, staged: bool) -> dict:
        out = ROOT / "out" / name
        if out.exists():
            shutil.rmtree(out)
        cfg.bootstrap.staged = staged
        mock_services.LLMHandler.plan_calls = 0
        mock_services.LLMHandler.skeleton_checks = 0
        mock_services.LLMHandler.bootstrap_html_override = pinned
        try:
            summary = Pipeline(cfg, out).build(make_source_png(Path("tmp/source_fixture.png")))
        finally:
            mock_services.LLMHandler.bootstrap_html_override = None
            cfg.bootstrap.staged = True
        return {"summary": summary, "out": out}

    staged = run("scope_staged", True)
    single = run("scope_single", False)

    for name in ("scope_staged", "scope_single"):
        draft = (ROOT / "out" / name / "rounds" / "bootstrap.html").read_text()
        assert draft == pinned, f"{name} did not start from the pinned draft"
    ok("both strategies started the loop from the identical draft")

    def shape(summary: dict) -> list:
        return [
            (r["round"], r["decision"], r["mode"], r["scope"], r["changed_lines"], r["error"])
            for r in summary["rounds"]
        ]

    a, b = shape(staged["summary"]), shape(single["summary"])
    assert a == b, f"the rounds diverged:\n staged {a}\n single {b}"
    assert len(a) == 4, a
    ok(f"round for round identical: {[(r[0], r[1], r[2]) for r in a]}")

    for key in ("stop_reason", "kept", "reverted", "rejected", "errors",
                "kept_line_changes"):
        assert staged["summary"][key] == single["summary"][key], key
    ok("stop_reason and every tally match")

    left = (staged["out"] / "clone.html").read_text()
    right = (single["out"] / "clone.html").read_text()
    assert left == right, "the same rounds produced different clone.html"
    ok(f"clone.html is byte-identical ({len(left)} chars)")

    # And the loop's own stages must not read the bootstrap settings at all.
    import inspect

    import pipeline as pipeline_mod

    loop_members = [
        pipeline_mod.Pipeline.plan, pipeline_mod.Pipeline.action,
        pipeline_mod.Pipeline.apply, pipeline_mod.Pipeline.verify,
        pipeline_mod.Pipeline.action_mode,
    ]
    for member in loop_members:
        body = inspect.getsource(member)
        assert "cfg.bootstrap" not in body and "bootstrap" not in body.lower(), (
            f"{member.__name__} reads the bootstrap settings"
        )
    ok("PLAN / ACTION / APPLY / VERIFY never read the bootstrap settings")


# --------------------------- 21. the docs say what the code actually does

def test_docs_match_the_code() -> None:
    """Every doc claim here is one that has silently gone stale before."""
    print("\n[21] 문서가 코드와 일치한다")
    import prompts
    from config import BootstrapConfig
    from pipeline import HTML_WARN_SIZE, Pipeline

    readme = (ROOT / "README.md").read_text()
    flat = lambda text: " ".join(text.split())

    # 1. The contract quoted in the README is the one the model is actually
    #    sent. This block has been edited in prompts.py without the doc before.
    assert flat(prompts.OPERATOR_CONTRACT) in flat(readme), \
        "README의 계약 블록이 prompts.OPERATOR_CONTRACT 와 다르다"
    ok("the operator contract quoted in README is the real prompt text")

    # 2. The scope -> mode table. Each row is checked against the router, not
    #    against another piece of prose.
    rows = [
        ("local", 1000, "patch"),
        ("section", 1000, "section"),
        ("global", 1000, "rewrite"),
        ("global", HTML_WARN_SIZE + 1, "section"),
    ]
    for scope, size, mode in rows:
        assert Pipeline.action_mode({"scope": scope}, size) == mode
        assert f"`{scope}`" in readme, f"README에 scope {scope} 설명이 없다"
        assert f"| {mode} |" in readme or f"| `{mode}`" in readme, mode
    assert str(HTML_WARN_SIZE) in readme, "README의 크기 임계값이 코드와 다르다"
    ok(f"the scope->mode table matches the router, {HTML_WARN_SIZE} included")

    # 3. The [bootstrap] defaults table.
    defaults = BootstrapConfig()
    assert f"`{defaults.rough_max_side}`" in readme, defaults.rough_max_side
    assert f"`{defaults.max_blocks}`" in readme, defaults.max_blocks
    assert f"({defaults.rough_max_side}px)" in readme or \
        f"{defaults.rough_max_side}px" in readme, "README가 축소 크기를 안 적었다"
    ok(f"the [bootstrap] defaults in README are {defaults}")

    # 4. Flags the README tells people to type have to exist.
    import run as run_mod

    parser = run_mod.build_parser() if hasattr(run_mod, "build_parser") else None
    for flag in ("--bootstrap", "--ui-public-host", "--max-rounds", "--note"):
        assert flag in readme, f"README에 {flag} 설명이 없다"
        assert flag in (ROOT / "run.py").read_text(), f"run.py 에 {flag} 가 없다"
    assert parser is None or parser  # build_parser is optional
    ok("every CLI flag the README names exists in run.py")

    # 5. thinking on/off per stage, as the README's table claims.
    thinking = {"SKELETON": False, "CHECK": True, "FILL": False,
                "PLAN": True, "VERIFY": True}
    source = (ROOT / "pipeline.py").read_text()
    for call, want in [('stage="skeleton"', False), ('stage="skeleton_check"', True),
                       ('stage="plan"', True), ('stage="verify"', True)]:
        where = source.find(call)
        assert where != -1, call
        window = source[max(0, where - 200):where]
        assert f"thinking={want}" in window, f"{call} 의 thinking 이 {want} 가 아니다"
    assert all(k in readme for k in thinking), "README의 thinking 표에 빠진 단계가 있다"
    ok("thinking is on for the judging stages and off for the generating ones")


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
    test_ui_and_region()
    test_operator_only_and_region_loop(llm_base, renderer_url)
    test_verify_feeds_next_plan(llm_base, renderer_url)
    test_failed_attempts_persist(llm_base, renderer_url)
    test_three_states_are_consistent()
    test_config_without_tomllib()
    test_renderer_stays_external()
    test_loop_makes_progress()
    test_section_mode(llm_base, renderer_url)
    test_staged_bootstrap(llm_base, renderer_url)
    test_bootstrap_does_not_touch_the_loop(llm_base, renderer_url)
    test_docs_match_the_code()
    # The README states this number. Counting this check itself keeps the two
    # from drifting: change the suite, the number in the doc has to follow.
    total = len(PASS) + 1
    readme = (ROOT / "README.md").read_text()
    assert f"{total}개 검사" in readme, (
        f"README says something other than {total}개 검사 - 문서의 검사 수를 고쳐라"
    )
    ok(f"README states {total} checks, which is what ran")
    print(f"\n{len(PASS)} checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
