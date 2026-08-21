"""Small shared helpers: logging, image encoding, text cleanup, JSON extraction."""

from __future__ import annotations

import base64
import io
import json
import logging
import os
import re
import sys
from pathlib import Path

LOG = logging.getLogger("docgen")


def setup_logging(verbose: bool = False, logfile: str | os.PathLike | None = None) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    root = logging.getLogger()
    root.setLevel(level)
    for handler in list(root.handlers):
        root.removeHandler(handler)

    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(message)s", "%H:%M:%S")
    stream = logging.StreamHandler(sys.stderr)
    stream.setFormatter(fmt)
    root.addHandler(stream)

    if logfile:
        Path(logfile).parent.mkdir(parents=True, exist_ok=True)
        fileh = logging.FileHandler(logfile, encoding="utf-8")
        fileh.setFormatter(fmt)
        root.addHandler(fileh)


# --------------------------------------------------------------------------- io

def ensure_dir(path: str | os.PathLike) -> Path:
    p = Path(path)
    p.mkdir(parents=True, exist_ok=True)
    return p


def write_text(path: str | os.PathLike, text: str) -> Path:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    return p


def write_bytes(path: str | os.PathLike, data: bytes) -> Path:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)
    return p


def write_json(path: str | os.PathLike, obj) -> Path:
    return write_text(path, json.dumps(obj, ensure_ascii=False, indent=2))


def read_text(path: str | os.PathLike) -> str:
    return Path(path).read_text(encoding="utf-8")


# ------------------------------------------------------------------------ image

def png_bytes(source: str | os.PathLike | bytes, max_side: int = 1600) -> bytes:
    """Normalise any image (path or bytes) to an RGB PNG, downscaled to max_side."""
    from PIL import Image

    if isinstance(source, (bytes, bytearray)):
        img = Image.open(io.BytesIO(bytes(source)))
    else:
        img = Image.open(str(source))

    img.load()
    if img.mode != "RGB":
        # Flatten transparency onto white so documents keep a paper-like background.
        if img.mode in ("RGBA", "LA", "P"):
            rgba = img.convert("RGBA")
            flat = Image.new("RGB", rgba.size, (255, 255, 255))
            flat.paste(rgba, mask=rgba.split()[-1])
            img = flat
        else:
            img = img.convert("RGB")

    longest = max(img.size)
    if max_side and longest > max_side:
        scale = max_side / float(longest)
        new_size = (max(1, round(img.width * scale)), max(1, round(img.height * scale)))
        img = img.resize(new_size, Image.LANCZOS)
        LOG.debug("image downscaled %s -> %s", longest, max(new_size))

    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


def data_uri(data: bytes, mime: str = "image/png") -> str:
    return f"data:{mime};base64," + base64.b64encode(data).decode("ascii")


def image_size(source: str | os.PathLike | bytes) -> tuple[int, int]:
    from PIL import Image

    if isinstance(source, (bytes, bytearray)):
        img = Image.open(io.BytesIO(bytes(source)))
    else:
        img = Image.open(str(source))
    return img.size


# ------------------------------------------------------------------------- text

_FENCE_RE = re.compile(r"^\s*```[a-zA-Z0-9_+-]*\s*\n(.*?)\n?\s*```\s*$", re.DOTALL)
_THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)
_OPEN_THINK_RE = re.compile(r"<think>.*\Z", re.DOTALL | re.IGNORECASE)


def strip_think(text: str) -> str:
    """Remove <think>...</think> blocks, including an unterminated trailing one."""
    if not text:
        return ""
    out = _THINK_RE.sub("", text)
    out = _OPEN_THINK_RE.sub("", out)
    return out.strip()


def strip_code_fences(text: str) -> str:
    """Strip a surrounding markdown fence; also handles leading prose + one fence."""
    if not text:
        return ""
    body = text.strip()

    match = _FENCE_RE.match(body)
    if match:
        return match.group(1).strip()

    # Fallback: take the largest fenced block if the model wrapped the HTML in prose.
    blocks = re.findall(r"```[a-zA-Z0-9_+-]*\s*\n(.*?)```", body, re.DOTALL)
    if blocks:
        return max(blocks, key=len).strip()

    return body


def clean_html_output(text: str) -> str:
    """Model output -> pure HTML string."""
    html = strip_code_fences(strip_think(text))
    # Some models prepend a sentence before the doctype; cut to the real start.
    lowered = html.lower()
    for marker in ("<!doctype", "<html"):
        idx = lowered.find(marker)
        if idx < 0:
            continue
        if idx > 0:
            html = html[idx:]
        break
    return html.strip()


def html_sanity_check(html: str, min_length: int = 200) -> tuple[bool, str]:
    """Minimal structural check from the spec: has a root tag, non-empty, not tiny."""
    if not html or not html.strip():
        return False, "html is empty"
    if len(html) < min_length:
        return False, f"html is suspiciously short ({len(html)} chars < {min_length})"
    lowered = html.lower()
    if "<!doctype" not in lowered and "<html" not in lowered:
        return False, "html has neither <!doctype nor <html"
    if "</html>" not in lowered and "</body>" not in lowered:
        return False, "html appears truncated (no closing </body> or </html>)"
    return True, "ok"


def extract_json(text: str) -> dict:
    """Pull the first balanced JSON object out of a model response."""
    body = strip_code_fences(strip_think(text))
    if not body:
        raise ValueError("empty response, no JSON found")

    try:
        parsed = json.loads(body)
        if isinstance(parsed, dict):
            return parsed
    except json.JSONDecodeError:
        pass

    start = body.find("{")
    while start != -1:
        depth = 0
        in_string = False
        escaped = False
        for idx in range(start, len(body)):
            ch = body[idx]
            if in_string:
                if escaped:
                    escaped = False
                elif ch == "\\":
                    escaped = True
                elif ch == '"':
                    in_string = False
                continue
            if ch == '"':
                in_string = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    chunk = body[start : idx + 1]
                    try:
                        parsed = json.loads(chunk)
                    except json.JSONDecodeError:
                        break
                    if isinstance(parsed, dict):
                        return parsed
                    break
        start = body.find("{", start + 1)

    raise ValueError(f"no JSON object found in response: {body[:300]!r}")


def truncate(text: str, limit: int) -> str:
    if text is None:
        return ""
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n<!-- truncated, {len(text) - limit} more chars -->"
