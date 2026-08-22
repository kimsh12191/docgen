"""TEST-ONLY stand-in for the external renderer service, backed by real Chromium.

The production pipeline never imports this: it only ever speaks HTTP to the
renderer service configured in config.toml. This module exists so the loop can
be exercised end to end in an environment that cannot reach the real service.
It implements the documented contract exactly:

    GET  /health -> {"ok": true, ...}
    POST /probe  {html, width, wait_ms, device_scale, probe_js}
                 -> {"ok": true, "png_base64": ..., "metrics": <probe_js result>}
"""

from __future__ import annotations

import base64
import json
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer

from playwright.sync_api import Error as PWError
from playwright.sync_api import sync_playwright

CHROMIUM = "/opt/pw-browsers/chromium"

_pw = None
_browser = None


def _browser_instance():
    global _pw, _browser
    if _browser is None:
        _pw = sync_playwright().start()
        _browser = _pw.chromium.launch(
            executable_path=CHROMIUM,
            args=["--no-sandbox", "--disable-dev-shm-usage", "--font-render-hinting=none"],
        )
    return _browser


def render(html: str, width: int, wait_ms: int, device_scale: float, probe_js: str):
    """Render HTML and evaluate probe_js in the page. Returns (png, metrics)."""
    browser = _browser_instance()
    ctx = browser.new_context(
        viewport={"width": int(width), "height": 1200},
        device_scale_factor=float(device_scale),
    )
    try:
        page = ctx.new_page()
        page.set_content(html, wait_until="load")
        page.wait_for_timeout(int(wait_ms))

        metrics = {}
        if probe_js:
            try:
                metrics = page.evaluate(probe_js)
            except PWError as exc:
                # Surface the failure instead of silently pretending it worked.
                metrics = {"probe_error": str(exc)[:500]}

        png = page.screenshot(full_page=True, type="png")
        return png, metrics
    finally:
        ctx.close()


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

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
        if self.path == "/health":
            self._send(200, {"ok": True, "service": "test-chromium-renderer"})
        else:
            self._send(404, {"ok": False, "error": "not found"})

    def do_POST(self):
        if self.path != "/probe":
            self._send(404, {"ok": False, "error": "not found"})
            return
        try:
            req = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))))
        except (ValueError, UnicodeDecodeError) as exc:
            self._send(400, {"ok": False, "error": f"bad json: {exc}", "metrics": {}})
            return

        missing = [k for k in ("html", "width", "wait_ms", "device_scale", "probe_js") if k not in req]
        if missing:
            self._send(400, {"ok": False, "error": f"missing fields: {missing}", "metrics": {}})
            return

        try:
            png, metrics = render(
                req["html"], req["width"], req["wait_ms"], req["device_scale"], req["probe_js"]
            )
        except Exception as exc:  # noqa: BLE001 - report any render failure over the contract
            self._send(200, {"ok": False, "error": f"{type(exc).__name__}: {exc}", "metrics": {}})
            return

        self._send(200, {"ok": True, "png_base64": base64.b64encode(png).decode("ascii"), "metrics": metrics})


def main():
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 38900
    _browser_instance()
    # Single-threaded on purpose: sync_playwright is bound to its creating thread.
    srv = HTTPServer(("127.0.0.1", port), Handler)
    print(f"chromium renderer listening on http://127.0.0.1:{port}", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
