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


def _label_font(size: int = 16):
    from PIL import ImageFont

    for candidate in (
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    ):
        if os.path.exists(candidate):
            try:
                return ImageFont.truetype(candidate, size)
            except OSError:
                pass
    return ImageFont.load_default()


def _content_bottom(img, threshold: int = 250) -> int:
    """Last row that has any non-white pixel."""
    grey = img.convert("L")
    width, height = grey.size
    px = grey.load()
    for y in range(height - 1, -1, -1):
        for x in range(0, width, 4):  # every 4th column is plenty for whitespace
            if px[x, y] < threshold:
                return y + 1
    return height


def side_by_side(panels: list[tuple[str, bytes | str]], path: str | os.PathLike) -> Path:
    """Compose labelled images into one PNG so a person can compare them at a glance.

    Labels must be ASCII: the environment may have no font with wider coverage,
    and a label rendered as boxes is worse than an English one.

    Trailing whitespace is cropped by the same amount on every panel, so the
    panels stay vertically comparable.
    """
    from PIL import Image, ImageDraw

    loaded = []
    for label, src in panels:
        if isinstance(src, (bytes, bytearray)):
            img = Image.open(io.BytesIO(bytes(src)))
        else:
            img = Image.open(str(src))
        loaded.append((label, img.convert("RGB")))

    keep = min(
        max(_content_bottom(img) for _, img in loaded) + 24,
        max(img.height for _, img in loaded),
    )
    loaded = [(label, img.crop((0, 0, img.width, min(keep, img.height)))) for label, img in loaded]

    font = _label_font()
    bar = 26
    gap = 14
    height = max(img.height for _, img in loaded) + bar
    width = sum(img.width for _, img in loaded) + gap * (len(loaded) - 1)

    sheet = Image.new("RGB", (width, height), (238, 238, 238))
    draw = ImageDraw.Draw(sheet)
    x = 0
    for label, img in loaded:
        draw.text((x + 6, 5), label, fill=(0, 0, 0), font=font)
        sheet.paste(img, (x, bar))
        draw.rectangle([x, bar, x + img.width - 1, bar + img.height - 1], outline=(150, 150, 150))
        x += img.width + gap

    return write_bytes(path, _to_png(sheet))


def _to_png(img) -> bytes:
    buf = io.BytesIO()
    img.save(buf, format="PNG")
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


def apply_edits(html: str, edits) -> tuple[str, list[str]]:
    """Apply exact search/replace edits in order.

    Every `find` must match exactly once at the moment it is applied. An edit
    that is missing, ambiguous, or a no-op raises instead of being skipped: a
    partially applied patch is worse than a rejected round, because VERIFY would
    then judge an edit that never fully happened.
    """
    if not isinstance(edits, list) or not edits:
        raise ValueError("patch contained no edits")

    out = html
    applied: list[str] = []
    for index, edit in enumerate(edits, 1):
        if not isinstance(edit, dict):
            raise ValueError(f"edit {index} is not an object")
        find = edit.get("find")
        replace = edit.get("replace")
        if not isinstance(find, str) or not find:
            raise ValueError(f"edit {index} has an empty 'find'")
        if not isinstance(replace, str):
            raise ValueError(f"edit {index} has a non-string 'replace'")
        if find == replace:
            raise ValueError(f"edit {index} is a no-op")

        hits = out.count(find)
        if hits == 0:
            raise ValueError(f"edit {index} 'find' is not in the document: {find[:100]!r}")
        if hits > 1:
            raise ValueError(
                f"edit {index} 'find' matches {hits} places, it must be unique: {find[:100]!r}"
            )

        out = out.replace(find, replace, 1)
        applied.append(f"{find[:60]!r} -> {replace[:60]!r}")

    return out, applied


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
