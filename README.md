# docgen

Turn a document PNG into an **editable HTML clone** that looks the same to a
human reading them side by side.

One loop, driven entirely by a Qwen VLM looking at real render output:

```
SOURCE PNG -> INITIAL HTML -> RENDER
                                |
        +-----------------------+
        v
      PLAN   (VLM, thinking ON)   pick the single biggest mismatch
      ACTION (VLM, thinking OFF)  rewrite the HTML to fix it
      APPLY  (Python)             de-fence + sanity check
      RENDER (external service)   HTML -> PNG
      VERIFY (VLM, thinking ON)   keep / revert / done
        |
        +-> keep   -> adopt candidate, next PLAN
            revert -> restore previous HTML, next PLAN
            done   -> clone.html
```

There is no CV pipeline, no heuristic rule set, no action DSL. The VLM sees the
source and the actual render, decides, edits, and then judges its own edit
against a fresh render.

## Requirements

* Python 3.11+ (uses `tomllib`); only third-party dependency is Pillow
* The **Qwen VLM** OpenAI-compatible endpoint
* The **HTML renderer** service (already running separately)

```bash
pip install -r requirements.txt
```

Both services live on a private network. Everything must be run from a host
that can reach `10.167.129.250:30164` and `10.167.129.230:30900`.

## Usage

```bash
python run.py doctor                          # check both services
python run.py doctor --llm-image              # also verify a multimodal call
python run.py render test.html -o test.png    # one-shot HTML -> PNG
python run.py build sample.png -o out/sample  # the full loop
python run.py build sample.png --max-rounds 4 -v
```

`doctor` exits non-zero if anything fails, and `build` refuses to start when the
renderer health check fails — the renderer is a hard dependency of the loop.

## Output

```
out/sample/
  clone.html          final editable HTML
  clone.png           its render
  final_verify.json   terminal VERIFY verdict
  summary.json        per-round decisions
  run.log
  rounds/
    bootstrap.html  bootstrap.png
    r01/  plan.json  action_raw.txt
          before.html  before.png
          candidate.html  candidate.png
          metrics.json  verify.json
    r02/  ...
```

A round that fails APPLY or RENDER writes `error.json` instead of
`candidate.png` and does not touch the current HTML.

## Configuration

`config.toml` holds the endpoints and loop settings. Three environment
variables override it for pointing at a relocated service:
`DOCGEN_LLM_BASE_URL`, `DOCGEN_LLM_MODEL`, `DOCGEN_RENDERER_URL`.

## Files

| file | role |
| --- | --- |
| `run.py` | CLI: `doctor`, `render`, `build` |
| `config.py` | `config.toml` loading |
| `llm.py` | Qwen client (stdlib `urllib`), message/image helpers |
| `renderer.py` | `/health` + `/probe` client and the probe script |
| `prompts.py` | the four stage prompts |
| `pipeline.py` | bootstrap + the PLAN/ACTION/APPLY/RENDER/VERIFY loop |
| `utils.py` | logging, image encoding, fence/think stripping, JSON extraction |

## Renderer contract

`POST /probe` is sent exactly these fields, `probe_js` always included:

```json
{"html": "...", "width": 800, "wait_ms": 400, "device_scale": 1.0, "probe_js": "() => {...}"}
```

and expects `{"ok": true, "png_base64": "...", "metrics": {...}}`. Anything
else raises `RendererError`. This project never starts its own browser.

## Thinking control

Thinking is set per stage via `chat_template_kwargs.enable_thinking`
(BOOTSTRAP off, PLAN on, ACTION off, VERIFY on). If the server answers HTTP 400
because of that key, the client drops it, retries once, logs a prominent
warning, and from then on follows the server default — `summary.json` records
this as `"thinking_control": false`.

## Offline tests

The service endpoints are not needed to exercise the loop's logic:

```bash
python3 tests/test_offline.py
```

This starts an in-process mock renderer and mock Qwen implementing the same HTTP
contracts, then drives a full build and asserts the artefact layout and the
keep / revert / reject / done semantics. `tests/mock_services.py` is test-only
and is never imported by the pipeline.
