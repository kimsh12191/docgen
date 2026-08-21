"""Client for the external HTML renderer service.

The renderer is an already-running container service. This project never spawns
its own Playwright/Chromium instance; it only speaks the /health and /probe HTTP
contract.
"""

from __future__ import annotations

import base64
import json
import logging
import urllib.error
import urllib.request

LOG = logging.getLogger("docgen.renderer")

# Minimal probe script. The renderer evaluates this in the page and returns the
# result as `metrics`. It only needs to expose the page box and coarse DOM bboxes.
PROBE_JS = """() => {
  const root =
    document.querySelector('.sheet') ||
    document.querySelector('.document-page') ||
    document.body;
  const rect = root.getBoundingClientRect();
  const elements = Array.from(
    document.querySelectorAll('table, tr, td, th, div, p, span')
  ).slice(0, 5000).map((el, index) => {
    const r = el.getBoundingClientRect();
    return {
      index,
      tag: el.tagName.toLowerCase(),
      id: el.id || '',
      className:
        typeof el.className === 'string' ? el.className : '',
      x: r.x,
      y: r.y,
      width: r.width,
      height: r.height,
      text: (el.innerText || '').slice(0, 200)
    };
  });
  return {
    page: {
      x: rect.x,
      y: rect.y,
      width: rect.width,
      height: rect.height
    },
    scrollWidth: document.documentElement.scrollWidth,
    scrollHeight: document.documentElement.scrollHeight,
    bodyWidth: document.body.scrollWidth,
    bodyHeight: document.body.scrollHeight,
    elements
  };
}"""

SMOKE_HTML = """<!doctype html>
<html>
<head>
<meta charset="utf-8">
<style>
  body { margin:0; background:white; }
  .sheet {
    width:800px;
    height:1000px;
    background:white;
    box-sizing:border-box;
    padding:40px;
  }
</style>
</head>
<body>
<div class="sheet">
  <h1>renderer smoke test</h1>
  <table border="1">
    <tr><td>A</td><td>B</td></tr>
  </table>
</div>
</body>
</html>
"""


class RendererError(RuntimeError):
    """Raised when the renderer is unreachable or refuses to produce a PNG."""


class RendererClient:
    def __init__(
        self,
        base_url: str = "http://10.167.129.230:30900",
        timeout: int = 180,
        device_scale: float = 1.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.device_scale = device_scale

    # ------------------------------------------------------------------ http

    def _request(self, path: str, payload: dict | None = None) -> dict:
        url = f"{self.base_url}{path}"
        if payload is None:
            req = urllib.request.Request(url, method="GET")
        else:
            body = json.dumps(payload).encode("utf-8")
            req = urllib.request.Request(
                url,
                data=body,
                method="POST",
                headers={"Content-Type": "application/json"},
            )

        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:500]
            raise RendererError(f"{path} HTTP {exc.code}: {detail}") from exc
        except urllib.error.URLError as exc:
            raise RendererError(f"{path} unreachable at {self.base_url}: {exc.reason}") from exc
        except TimeoutError as exc:
            raise RendererError(f"{path} timed out after {self.timeout}s") from exc

        try:
            return json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RendererError(f"{path} returned non-JSON body: {raw[:200]!r}") from exc

    # ------------------------------------------------------------------- api

    def health(self) -> dict:
        """GET /health. Raises RendererError when the service is not usable."""
        data = self._request("/health")
        if isinstance(data, dict) and data.get("ok") is False:
            raise RendererError(f"/health reported not ok: {data}")
        LOG.debug("renderer health: %s", data)
        return data if isinstance(data, dict) else {"raw": data}

    def probe(
        self,
        html: str,
        width: int = 800,
        wait_ms: int = 400,
        probe_js: str | None = None,
    ) -> tuple[bytes, dict]:
        """POST /probe. Returns (png_bytes, metrics)."""
        payload = {
            "html": html,
            "width": width,
            "wait_ms": wait_ms,
            "device_scale": self.device_scale,
            "probe_js": probe_js if probe_js is not None else PROBE_JS,
        }
        data = self._request("/probe", payload)

        if not isinstance(data, dict):
            raise RendererError(f"/probe returned unexpected payload type {type(data).__name__}")
        if not data.get("ok", False):
            raise RendererError(f"/probe failed: {data.get('error', 'unknown error')}")

        b64 = data.get("png_base64")
        if not b64:
            raise RendererError("/probe returned ok but no png_base64")
        try:
            png = base64.b64decode(b64)
        except (ValueError, TypeError) as exc:
            raise RendererError(f"/probe png_base64 is not valid base64: {exc}") from exc
        if not png:
            raise RendererError("/probe returned an empty PNG")

        metrics = data.get("metrics", {})
        if not isinstance(metrics, dict):
            metrics = {"raw": metrics}
        LOG.debug("probe ok: %d bytes png, %d metric keys", len(png), len(metrics))
        return png, metrics

    def smoke(self, width: int = 800, wait_ms: int = 400) -> tuple[bytes, dict]:
        """Render the fixed smoke-test HTML to verify the service end to end."""
        return self.probe(SMOKE_HTML, width=width, wait_ms=wait_ms)
