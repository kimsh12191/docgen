"""Offline test doubles for the renderer and the Qwen endpoint.

TEST-ONLY. Nothing in the pipeline imports this module. The mock renderer is a
crude Pillow rasteriser that exists purely to return a valid, HTML-dependent
PNG over the real /probe contract -- it is not a substitute for the real
renderer service and the pipeline never grows its own renderer.
"""

from __future__ import annotations

import base64
import io
import json
import os
import re
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from PIL import Image, ImageDraw

MODEL = "qwen3.5_397b_a17b"


# ------------------------------------------------------------- crude rasteriser

def _visible_lines(html: str) -> list[str]:
    body = re.sub(r"(?is)<(script|style|head)\b.*?</\1>", " ", html)
    body = re.sub(r"(?i)<(br|/tr|/p|/h[1-6]|/div|/li)\s*/?>", "\n", body)
    body = re.sub(r"(?i)</t[dh]>", " | ", body)
    body = re.sub(r"(?s)<[^>]+>", "", body)
    body = body.replace("&nbsp;", " ").replace("&amp;", "&")
    lines = [re.sub(r"[ \t]+", " ", ln).strip(" |").strip() for ln in body.split("\n")]
    return [ln for ln in lines if ln]


def rasterise(html: str, width: int, device_scale: float) -> tuple[bytes, dict]:
    lines = _visible_lines(html)
    line_h = 18
    pad = 40
    height = max(200, pad * 2 + line_h * max(1, len(lines)))
    size = (int(width * device_scale), int(height * device_scale))

    img = Image.new("RGB", size, (255, 255, 255))
    draw = ImageDraw.Draw(img)
    draw.rectangle([2, 2, size[0] - 3, size[1] - 3], outline=(210, 210, 210))

    elements = []
    y = pad
    for index, line in enumerate(lines[:400]):
        draw.text((pad, y), line[:110], fill=(20, 20, 20))
        elements.append(
            {
                "index": index,
                "tag": "div",
                "id": "",
                "className": "",
                "x": float(pad),
                "y": float(y),
                "width": float(width - 2 * pad),
                "height": float(line_h),
                "text": line[:200],
            }
        )
        y += line_h

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    metrics = {
        "page": {"x": 0.0, "y": 0.0, "width": float(width), "height": float(height)},
        "scrollWidth": width,
        "scrollHeight": height,
        "bodyWidth": width,
        "bodyHeight": height,
        "elements": elements,
    }
    return buf.getvalue(), metrics


# --------------------------------------------------------------- mock renderer

class RendererHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):  # silence
        pass

    def _send(self, code: int, payload: dict):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/health":
            self._send(200, {"ok": True, "service": "mock-renderer"})
        else:
            self._send(404, {"ok": False, "error": "not found"})

    def do_POST(self):
        if self.path != "/probe":
            self._send(404, {"ok": False, "error": "not found"})
            return
        length = int(self.headers.get("Content-Length", "0"))
        try:
            req = json.loads(self.rfile.read(length).decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            self._send(400, {"ok": False, "error": f"bad json: {exc}", "metrics": {}})
            return

        # Enforce the documented contract so a client regression is caught here.
        missing = [k for k in ("html", "width", "wait_ms", "device_scale", "probe_js") if k not in req]
        if missing:
            self._send(400, {"ok": False, "error": f"missing fields: {missing}", "metrics": {}})
            return
        if not isinstance(req["probe_js"], str) or "=>" not in req["probe_js"]:
            self._send(400, {"ok": False, "error": "probe_js must be a JS arrow function", "metrics": {}})
            return
        html = req["html"]
        if not isinstance(html, str) or not html.strip():
            self._send(200, {"ok": False, "error": "empty html", "metrics": {}})
            return

        png, metrics = rasterise(html, int(req["width"]), float(req["device_scale"]))
        self._send(200, {"ok": True, "png_base64": base64.b64encode(png).decode("ascii"), "metrics": metrics})


# -------------------------------------------------------------------- mock llm

GOOD_HTML = """<!doctype html>
<html>
<head><meta charset="utf-8"><style>
body {{ margin:0; background:#fff; font-family: sans-serif; }}
.sheet {{ width:800px; box-sizing:border-box; padding:40px; background:#fff; }}
h1 {{ font-size:{title}px; margin:0 0 16px 0; }}
table {{ border-collapse:collapse; width:{table_width}; }}
td, th {{ border:1px solid #333; padding:6px 10px; font-size:13px; }}
</style></head>
<body>
<div class="sheet">
  <h1>Quarterly Expense Report</h1>
  <p>Prepared for the finance committee, revision {rev}.</p>
  <table>
    <tr><th>Item</th><th>Q1</th><th>Q2</th></tr>
    <tr><td>Travel</td><td>1,200</td><td>1,450</td></tr>
    <tr><td>Equipment</td><td>3,400</td><td>2,900</td></tr>
    <tr><td>Total</td><td>4,600</td><td>4,350</td></tr>
  </table>
  <p>Notes: figures are provisional and subject to audit.</p>
</div>
</body>
</html>"""


def _plan_json(target: str, goal: str, scope: str = "global") -> str:
    return json.dumps(
        {
            "scope": scope,
            "target": target,
            "problem": "the render does not match the source",
            "cause": "css sizing",
            "goal": goal,
        }
    )


def _verify_json(decision: str, reason: str) -> str:
    return json.dumps(
        {
            "goal_achieved": decision != "revert",
            "improved": decision != "revert",
            "regression": decision == "revert",
            "decision": decision,
            "reason": reason,
            "next_major_issue": "" if decision == "done" else "table width",
        }
    )


class LLMHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    plan_calls = 0
    lock = threading.Lock()
    reject_thinking = os.environ.get("MOCK_LLM_REJECT_THINKING") == "1"
    # Test knobs for the truncation cases.
    force_length_on_action = False
    bootstrap_html_override = None
    last_action_html_len = None

    def log_message(self, *args):
        pass

    def _send(self, code: int, payload: dict):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.endswith("/models"):
            self._send(200, {"object": "list", "data": [{"id": MODEL, "object": "model"}]})
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        req = json.loads(self.rfile.read(length).decode("utf-8"))

        if self.reject_thinking and "chat_template_kwargs" in req:
            self._send(400, {"error": {"message": "unexpected keyword argument chat_template_kwargs"}})
            return

        text = ""
        n_images = 0
        for msg in req.get("messages", []):
            content = msg.get("content")
            if isinstance(content, str):
                text += content + "\n"
            else:
                for part in content or []:
                    if part.get("type") == "text":
                        text += part["text"] + "\n"
                    elif part.get("type") == "image_url":
                        n_images += 1
                        assert part["image_url"]["url"].startswith("data:image/png;base64,")

        content, stage = self._respond(text, n_images)
        finish = "length" if (stage == "action" and LLMHandler.force_length_on_action) else "stop"
        self._send(
            200,
            {
                "id": "mock",
                "object": "chat.completion",
                "model": MODEL,
                "choices": [{"index": 0, "message": {"role": "assistant", "content": content}, "finish_reason": finish}],
                "usage": {"prompt_tokens": 100, "completion_tokens": len(content) // 4},
                "_stage": stage,
            },
        )

    def _respond(self, text: str, n_images: int) -> tuple[str, str]:
        if "Write HTML that recreates it" in text:
            assert n_images == 1, f"bootstrap must send 1 image, got {n_images}"
            if LLMHandler.bootstrap_html_override:
                return LLMHandler.bootstrap_html_override, "bootstrap"
            body = GOOD_HTML.format(title=28, table_width="60%", rev=0)
            return "```html\n" + body + "\n```", "bootstrap"

        if "Find the single most important mismatch" in text:
            assert n_images == 2, f"plan must send 2 images, got {n_images}"
            with LLMHandler.lock:
                LLMHandler.plan_calls += 1
                n = LLMHandler.plan_calls
            # Rounds 1, 2, 4 are local (patch path); round 3 is global (rewrite path).
            scope = "global" if n == 3 else "local"
            return (
                "<think>looking at both images closely</think>"
                + _plan_json(f"target-{n}", f"goal number {n}", scope),
                "plan",
            )

        if "Apply the plan" in text:
            # 2 normally; 4 when the operator marked a region (source+render crops).
            assert n_images in (2, 4), f"action must send 2 or 4 images, got {n_images}"
            # Record how much of the document ACTION actually received.
            start = text.find("```html\n")
            end = text.find("\n```", start)
            LLMHandler.last_action_html_len = end - start - 8 if start >= 0 and end > start else -1
            n = LLMHandler.plan_calls
            patch_mode = "Express the edit as exact string replacements" in text

            if patch_mode:
                if n == 2:
                    # 'find' that is not in the document -> APPLY must reject.
                    edits = [{"find": "THIS_STRING_IS_NOT_IN_THE_DOCUMENT", "replace": "x"}]
                elif n == 1:
                    edits = [{"find": "font-size:28px", "replace": "font-size:30px"}]
                else:
                    edits = [{"find": "revision 0", "replace": "revision 1"}]
                return "```json\n" + json.dumps({"edits": edits}) + "\n```", "action"

            body = GOOD_HTML.format(title=28 + n * 2, table_width=f"{60 + n * 8}%", rev=n)
            return "```html\n" + body + "\n```", "action"

        if "Choose exactly one" in text:
            assert n_images == 3, f"verify must send 3 images, got {n_images}"
            n = LLMHandler.plan_calls
            if n == 1:
                return _verify_json("keep", "title size now matches"), "verify"
            if n == 3:
                return _verify_json("revert", "table became too wide"), "verify"
            return _verify_json("done", "essentially the same document"), "verify"

        return "ok", "other"


# --------------------------------------------------------------------- runners

def start(handler_cls, port: int = 0) -> tuple[ThreadingHTTPServer, str]:
    server = ThreadingHTTPServer(("127.0.0.1", port), handler_cls)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, f"http://127.0.0.1:{server.server_address[1]}"


if __name__ == "__main__":
    which = sys.argv[1] if len(sys.argv) > 1 else "renderer"
    port = int(sys.argv[2]) if len(sys.argv) > 2 else 0
    cls = RendererHandler if which == "renderer" else LLMHandler
    srv, url = start(cls, port)
    print(f"{which} listening on {url}", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
