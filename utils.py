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


def crop_normalized(source, rect: dict, margin: float = 0.03) -> bytes:
    """Crop a normalised rect (0..1) out of an image, with a little context.

    The rect is scale-free, so the same one applies to a source scan and to an
    800px render even though their pixel sizes differ.
    """
    from PIL import Image

    if isinstance(source, (bytes, bytearray)):
        img = Image.open(io.BytesIO(bytes(source)))
    else:
        img = Image.open(str(source))
    img = img.convert("RGB")

    def span(start: float, size: float, total: int) -> tuple[int, int]:
        lo = max(0.0, start - margin)
        hi = min(1.0, start + size + margin)
        a, b = int(lo * total), int(hi * total)
        if b - a < 8:  # never hand back a sliver
            b = min(total, a + 8)
        return a, b

    x0, x1 = span(float(rect.get("x", 0)), float(rect.get("w", 1)), img.width)
    y0, y1 = span(float(rect.get("y", 0)), float(rect.get("h", 1)), img.height)
    return _to_png(img.crop((x0, y0, x1, y1)))


def target_page_size(source: str | os.PathLike | bytes, width: int) -> tuple[int, int]:
    """The page size the recreation should render at, in CSS pixels.

    The renderer always lays out at a fixed width, so the source's aspect ratio
    is what fixes the height. Without this the model is asked to match a page
    whose intended height is nowhere stated, and a render half again too tall
    looks no different from a correct one when the two images it compares are
    at different scales anyway.
    """
    from PIL import Image

    if isinstance(source, (bytes, bytearray)):
        img = Image.open(io.BytesIO(bytes(source)))
    else:
        img = Image.open(str(source))
    src_w, src_h = img.size
    if src_w <= 0 or src_h <= 0:
        raise ValueError(f"source image has no size: {src_w}x{src_h}")
    return int(width), max(1, round(width * src_h / float(src_w)))


def resize_to_width(source: str | os.PathLike | bytes, width: int) -> bytes:
    """The same image at a given pixel width, so two images can be compared.

    Comparing a 2480px scan with an 800px render asks the model to judge
    proportions across a 3x scale difference. Putting both at one width makes
    "too tall" and "too wide" visible instead of inferable.
    """
    from PIL import Image

    if isinstance(source, (bytes, bytearray)):
        img = Image.open(io.BytesIO(bytes(source)))
    else:
        img = Image.open(str(source))
    img.load()
    if img.mode != "RGB":
        img = img.convert("RGB")
    if img.width != width and img.width > 0:
        height = max(1, round(img.height * width / float(img.width)))
        img = img.resize((width, height), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def clip(text: str, limit: int) -> str:
    """Long text with the middle removed, keeping both ends.

    For reading in a terminal. The head says what was asked and the tail says
    how it finished; the omitted middle is usually a document body, and the
    count says how much was left out so nothing looks complete when it is not.
    """
    text = text or ""
    if limit <= 0 or len(text) <= limit:
        return text
    head = limit * 2 // 3
    tail = limit - head
    cut = len(text) - head - tail
    return f"{text[:head]}\n\n... [{cut} chars omitted] ...\n\n{text[-tail:]}"


def transcribe_messages(messages: list) -> str:
    """Messages as readable text, images reduced to their dimensions.

    The point is to be able to read exactly what a stage was sent. Base64 image
    payloads are megabytes of noise, so each becomes one line naming its size --
    which is itself worth seeing, since a scale mismatch between two images is
    invisible in the prompt text.
    """
    from PIL import Image

    out: list[str] = []
    for message in messages or []:
        role = message.get("role", "?") if isinstance(message, dict) else "?"
        content = message.get("content") if isinstance(message, dict) else None
        if isinstance(content, str):
            out.append(f"--- {role} ---\n{content}")
            continue
        images = 0
        chunks: list[str] = []
        for part in content or []:
            if not isinstance(part, dict):
                continue
            if part.get("type") == "text":
                chunks.append(str(part.get("text", "")))
                continue
            url = (part.get("image_url") or {}).get("url", "")
            images += 1
            head, _, b64 = url.partition(",")
            try:
                blob = base64.b64decode(b64)
                size = "x".join(str(n) for n in Image.open(io.BytesIO(blob)).size)
                chunks.append(f"[image {images}: {size}, {len(blob) / 1024:.0f} KB]")
            except Exception:  # noqa: BLE001 - a placeholder must never break logging
                chunks.append(f"[image {images}: unreadable, {head[:40]}]")
        body = "\n\n".join(c for c in chunks if c)
        out.append(f"--- {role} ({images} image(s)) ---\n{body}")
    return "\n\n".join(out)


def side_by_side(
    panels: list[tuple[str, bytes | str]], path: str | os.PathLike
) -> tuple[Path, list[dict]]:
    """Compose labelled images into one PNG so a person can compare them at a glance.

    Labels must be ASCII: the environment may have no font with wider coverage,
    and a label rendered as boxes is worse than an English one.

    Panels are first scaled to a common width. Pasting a 2480px scan beside an
    800px render made the two impossible to compare -- and made the shared
    trailing-whitespace crop below meaningless, since a row of pixels meant a
    different amount of page in each panel.

    Returns (path, panel boxes). The boxes let a UI map a point on the composed
    sheet back to a position within one panel.
    """
    from PIL import Image, ImageDraw

    loaded = []
    for label, src in panels:
        if isinstance(src, (bytes, bytearray)):
            img = Image.open(io.BytesIO(bytes(src)))
        else:
            img = Image.open(str(src))
        loaded.append((label, img.convert("RGB")))

    # Scale to the narrowest panel: never upscale, so nothing is blurred to
    # match something else.
    common = min(img.width for _, img in loaded)
    loaded = [
        (
            label,
            img if img.width == common else img.resize(
                (common, max(1, round(img.height * common / float(img.width)))),
                Image.LANCZOS,
            ),
        )
        for label, img in loaded
    ]

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
    boxes: list[dict] = []
    x = 0
    for label, img in loaded:
        draw.text((x + 6, 5), label, fill=(0, 0, 0), font=font)
        sheet.paste(img, (x, bar))
        draw.rectangle([x, bar, x + img.width - 1, bar + img.height - 1], outline=(150, 150, 150))
        boxes.append({"label": label, "x": x, "y": bar, "width": img.width, "height": img.height})
        x += img.width + gap

    return write_bytes(path, _to_png(sheet)), boxes


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


def clean_fragment_output(text: str, tag: str) -> str:
    """Model output -> one HTML element. Unlike a full document there is no
    doctype to cut to, so the element's own tag is the anchor."""
    body = strip_code_fences(strip_think(text)).strip()
    start = body.find(f"<{tag}")
    if start > 0:
        body = body[start:]
    close = f"</{tag}>"
    end = body.rfind(close)
    if end != -1:
        body = body[: end + len(close)]
    return body.strip()


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


def apply_section(html: str, edit) -> tuple[str, str]:
    """Replace one whole block, located by a start and an end anchor.

    Patch mode would need the model to copy the entire span into `find`, which
    is hopeless for a fifty-row table; rewrite mode would need it to re-emit the
    whole document. Here it copies two short anchors instead and Python works
    out the span between them, so the response carries only the new block and
    the block may be restructured completely.
    """
    if not isinstance(edit, dict):
        raise ValueError("section edit is not an object")
    start = edit.get("find_start")
    end = edit.get("find_end")
    replace = edit.get("replace")
    for name, value in (("find_start", start), ("find_end", end), ("replace", replace)):
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"section edit has an empty or non-string {name!r}")

    hits = html.count(start)
    if hits == 0:
        raise ValueError(f"section 'find_start' is not in the document: {start[:100]!r}")
    if hits > 1:
        raise ValueError(
            f"section 'find_start' matches {hits} places, it must be unique: {start[:100]!r}"
        )

    begin = html.index(start)
    # Searched after the start anchor, so a closing tag that also occurs earlier
    # in the document does not pick the wrong span.
    tail = html.find(end, begin + len(start))
    if tail == -1:
        raise ValueError(
            f"section 'find_end' does not occur after 'find_start': {end[:100]!r}"
        )
    stop = tail + len(end)

    section = html[begin:stop]
    if section == replace:
        raise ValueError("section edit asks for no change")
    return html[:begin] + replace + html[stop:], f"{len(section)} -> {len(replace)} chars"


_BLOCK_TAG = re.compile(r'<([a-zA-Z][\w-]*)\b[^>]*\bdata-block="([^"]*)"[^>]*>')
_BLOCK_ROLE = re.compile(r'\bdata-role="([^"]*)"')


def block_markers(html: str) -> list[dict]:
    """The blocks a skeleton marked for the fill phase, in document order.

    Reads an attribute this project asked the model to emit; it is not an HTML
    parser and does not try to be. A repeated id is dropped rather than filled
    twice, because the opening tag would no longer identify one block.
    """
    out: list[dict] = []
    seen = set()
    for match in _BLOCK_TAG.finditer(html):
        tag, ident = match.group(1), match.group(2).strip()
        if not ident or ident in seen:
            if ident:
                LOG.warning("skeleton reuses data-block=%r; only the first is filled", ident)
            continue
        seen.add(ident)
        role = _BLOCK_ROLE.search(match.group(0))
        out.append({
            "id": ident,
            "tag": tag,
            "open": match.group(0),
            "role": (role.group(1).strip() if role else ""),
        })
    return out


def fill_block(html: str, marker: dict, replacement: str) -> tuple[str, str]:
    """Swap one marked block for its filled-in version.

    The anchors come from the marker rather than from the model, so a fill can
    never fail by mis-copying them. What the model does have to keep is the
    data-block attribute: without it the block disappears from the fill list and
    from every later diagnostic.
    """
    open_tag, tag, ident = marker["open"], marker["tag"], marker["id"]
    close = f"</{tag}>"
    if f'data-block="{ident}"' not in replacement:
        raise ValueError(f"block {ident} came back without its data-block attribute")

    begin = html.find(open_tag)
    if begin == -1:
        raise ValueError(f"block {ident} opening tag is no longer in the document")
    inner_start = begin + len(open_tag)
    stop = html.find(close, inner_start)
    if stop == -1:
        raise ValueError(f"block {ident} is never closed by {close}")
    # First-closing-tag-wins is only correct while blocks are siblings, which is
    # what the skeleton prompt demands. If one nested anyway, say so instead of
    # splicing the wrong span.
    if f"<{tag}" in html[inner_start:stop]:
        raise ValueError(f"block {ident} has a nested <{tag}>, so its extent is ambiguous")

    return apply_section(html, {
        "find_start": open_tag, "find_end": close, "replace": replacement,
    })


def diff_line_count(before: str, after: str) -> int:
    """How many lines differ between two documents.

    Length alone is a bad measure of an edit -- swapping 28px for 31px moves
    nothing on the ruler. This counts changed lines instead, so a round that
    only reflows attributes still registers as work.
    """
    import difflib

    diff = difflib.unified_diff(before.splitlines(), after.splitlines(), n=0, lineterm="")
    return sum(
        1 for line in diff if line[:1] in "+-" and not line.startswith(("+++", "---"))
    )


def apply_edits(html: str, edits) -> tuple[str, list[str]]:
    """Apply exact search/replace edits in order.

    Every `find` must match exactly once at the moment it is applied. An edit
    that is missing or ambiguous raises instead of being skipped: a partially
    applied patch is worse than a rejected round, because VERIFY would then
    judge an edit that never fully happened.

    An edit whose `replace` equals its `find` is the one exception. It asks for
    nothing, so dropping it leaves the rest of the patch exactly as the model
    intended -- and failing the whole round over a restated line throws away the
    real edits sitting next to it. A patch of nothing but no-ops still raises,
    because then there is no edit at all.
    """
    if not isinstance(edits, list) or not edits:
        raise ValueError("patch contained no edits")

    out = html
    applied: list[str] = []
    noops = 0
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
            LOG.debug("edit %d asks for no change, dropped: %r", index, find[:60])
            noops += 1
            continue

        hits = out.count(find)
        if hits == 0:
            raise ValueError(f"edit {index} 'find' is not in the document: {find[:100]!r}")
        if hits > 1:
            raise ValueError(
                f"edit {index} 'find' matches {hits} places, it must be unique: {find[:100]!r}"
            )

        out = out.replace(find, replace, 1)
        applied.append(f"{find[:60]!r} -> {replace[:60]!r}")

    if not applied:
        raise ValueError(f"patch asked for no change at all ({noops} no-op edit(s))")
    if noops:
        LOG.info("APPLY: dropped %d no-op edit(s) of %d", noops, len(edits))

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
