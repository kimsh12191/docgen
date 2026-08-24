"""Prompt text for the four VLM stages. Deliberately short."""

from __future__ import annotations

HISTORY_RULE = (
    "A history line may carry what the previous verdict concluded: \"why\" is the\n"
    "reason an edit was reverted, \"next\" is what was thought to still be wrong,\n"
    "and the tag in brackets says who judged it.\n"
    "Treat previous attempts as history, not facts.\n"
    "Judge the current images first."
)

BOOTSTRAP_SYSTEM = (
    "You recreate document images as clean, editable, self-contained HTML."
)

BOOTSTRAP_USER = """The image is a document page.
Write HTML that recreates it as closely as you reasonably can in one pass.

Rules:
- Return one complete HTML document: <!doctype html> ... </html>.
- Everything inline. No external CSS, fonts, images or scripts.
- Wrap the page in a single root element with class "sheet", laid out for a
  {width}px viewport width.
- Reproduce all visible text content, and the overall structure: headings,
  paragraphs, tables, columns, borders, alignment, relative font sizes.
- Use real <table> markup for anything that looks like a table or a form grid.
- Keep the markup editable: semantic tags and readable CSS, no absolute
  positioning of every element, no base64 blobs.

This is a first draft. It does not need to be pixel perfect; it needs to contain
all the content and the correct large-scale structure so it can be refined later.

Return only the HTML, nothing else."""


# --------------------------------------------------------------- staged bootstrap
#
# One call cannot get a dense page both structurally and typographically right,
# and PLAN/ACTION afterwards fix one thing a round, so a bad draft is never
# caught up with. These four prompts split the first draft into: get the layout
# right while the detail is invisible, check that much by eye, then fill the
# blocks in one at a time.

SKELETON_SYSTEM = (
    "You lay out document pages as HTML skeletons, structure first."
)

SKELETON_USER = """The image is a document page, shown at low resolution on purpose.
At this size you can see the layout but not the words. That is what to build.

Write the page's STRUCTURE as HTML: where the blocks sit, how big they are, how
they are aligned, which are bordered, which are tables and how many rows and
columns they have. Do not try to transcribe the text - a few words per block is
enough to show what belongs there.

Rules:
- Return one complete HTML document: <!doctype html> ... </html>.
- Everything inline. No external CSS, fonts, images or scripts.
- Wrap the page in a single root element with class "sheet", laid out for a
  {width}px viewport width.
- Divide the page into its major blocks, and mark EVERY block like this:
    <section data-block="1" data-role="short description of what goes here">
  Number them from 1 in reading order. The description is for the next step,
  so say what the block is ("문서 상단 제목과 문서번호", "지출 항목 표 4열").
- NEVER put a <section> inside another <section>. Blocks are siblings, so that
  each one's extent is unambiguous. Use div, table, p and so on inside a block.
- Aim for {max_blocks} blocks or fewer. A block is a region a person would
  point at, not a paragraph.
- Real sizes and borders: this has to render to roughly the right shape.

Return only the HTML, nothing else."""

SKELETON_CHECK_SYSTEM = "You compare page layouts at a glance."

SKELETON_CHECK_USER = """Both images are deliberately shown at low resolution.
Image 1 is the source document. Image 2 is a render of the HTML skeleton.

Ignore the text, the fonts and every small detail - at this size they are not
the question. Judge only the large-scale layout: the number and order of blocks,
their proportions and positions, column structure, table row and column counts,
which regions are bordered, how much whitespace sits where.

Squint at them. Would a person say these are the same page laid out the same way?

Return JSON only:
{{
  "matches": true,
  "problems": ["each structural difference worth fixing, largest first"],
  "goal": "what a single corrective edit should achieve, or empty if it matches"
}}"""

SKELETON_FIX_USER = """Both images are shown at low resolution on purpose.
Image 1 is the source document. Image 2 is a render of the HTML below.

The structure does not match yet. What is wrong:
{problems}

What the fix should achieve:
{goal}

Here is the current skeleton HTML:
```html
{html}
```

Fix the LAYOUT. Move, resize, split, merge or add blocks as the source requires;
change the table row and column counts if they are wrong. Do not start
transcribing text - the next step does that.

Keep every block marked as <section data-block="N" data-role="..."> and keep
blocks as siblings, never nested. Renumber them in reading order if you add or
remove any.

Return the complete modified HTML document, and nothing else."""

FILL_SYSTEM = "You fill in one block of a document recreation, exactly."

FILL_USER = """Image 1 is the source document, at full resolution.
Image 2 is the current render of the HTML below.

Your job is ONE block of this page:
  block {block_id} - {role}

Here is the current HTML:
```html
{html}
```

Rewrite that one block so it matches the source: every word of its text, its
real font sizes and weights, alignment, borders, padding, column widths, and
all of its rows and cells. This is the detail pass, so be exact rather than
conservative - inside this block, change whatever the source requires.

Do not touch anything outside the block. Do not change the page's overall
layout; if the block is in the wrong place, fill it in correctly anyway and
leave that for later.

Return the complete block element, starting with its own opening tag and ending
with its closing tag. Keep data-block="{block_id}" on that opening tag.

Return only that block's HTML, nothing else."""

PLAN_SYSTEM = "You are a meticulous visual diff analyst."

PLAN_USER = """You are improving an HTML recreation of a document image.
Image 1 is the source document.
Image 2 is the current HTML render.
Inspect both images carefully.
Find the single most important mismatch that should be fixed next.
Prefer a higher-level cause that explains multiple visible symptoms
instead of listing many tiny differences.
One round fixes one problem, but it fixes it EVERYWHERE it appears.
If the same mismatch shows up in ten table rows, the goal is all ten rows,
not one of them. Say so in the goal.

Decide how much of the document has to change, and put that in "scope":
- "local": the structure is right, some properties are wrong.
- "section": one block - a table, a header, a column, a stamp area - is built
  wrong and has to be rebuilt. Name that block in "target".
- "global": the whole page layout is wrong.
Prefer "section" over "local" whenever the structure inside a block is wrong.
Adjusting properties cannot fix a block that is built the wrong way, and a
round spent adjusting them is a round wasted.
Do not write HTML yet.
Return JSON only:
{
  "scope": "global | section | local",
  "target": "short description of the target",
  "problem": "what visibly differs",
  "cause": "most likely cause",
  "goal": "what the next edit should achieve"
}"""

ACTION_SYSTEM = "You edit HTML precisely and conservatively."

# Local edits come back as exact string replacements, so the response size
# tracks the size of the edit instead of the size of the document.
ACTION_PATCH_USER = """Image 1 is the source document.
Image 2 is the current HTML render.

This is the plan for the next edit:
{plan}

Here is the current HTML:
```html
{html}
```

Apply the plan by editing the HTML.

Change only what is necessary to achieve the plan.
Preserve parts that already match.

Express the edit as exact string replacements.
Each "find" must appear EXACTLY ONCE in the HTML above.
Copy it verbatim, including whitespace and punctuation.
Keep each "find" as short as possible while still being unique - normally a
single CSS declaration, one attribute, one tag, or one table row.
Do not return the whole document.

Short "find" strings are about keeping the response small, not about making the
edit small. Return one edit for every place the plan's goal applies: if the
problem appears in ten rows, return ten edits. A patch that fixes one instance
and leaves the other nine is a wasted round.

Return JSON only:
{{
  "edits": [
    {{"find": "exact text taken from the HTML above", "replace": "text to put in its place"}}
  ]
}}"""

ACTION_REWRITE_USER = """Image 1 is the source document.
Image 2 is the current HTML render.

This is the plan for the next edit:
{plan}

Here is the current HTML:
```html
{html}
```

Apply the plan to the HTML.

Change only what is necessary to achieve the plan.
Preserve parts that already match.

Return the complete modified HTML document, and nothing else."""


# The middle mode. Patch cannot restructure (the model would have to copy a
# fifty-row table verbatim into "find") and rewrite cannot fit a dense document
# inside max_tokens. Here the model copies two short anchors and Python works
# out the span between them, so the response carries only the new block.
ACTION_SECTION_USER = """Image 1 is the source document.
Image 2 is the current HTML render.

This is the plan for the next edit:
{plan}

Here is the current HTML:
```html
{html}
```

Rebuild one block of this document so that it matches the source.

Pick the smallest block that contains the whole problem - one table, one header,
one column, one section - and write that block again from scratch. Inside that
block you may change the structure, the markup and the CSS as much as the source
requires. This is NOT a minimal edit: inside the block, make it right.
Leave everything outside the block exactly as it is.

Identify the block with two short anchors copied verbatim from the HTML above:
- "find_start": the block's opening tag, for example <table class="grid">.
  It must appear EXACTLY ONCE in the HTML above.
- "find_end": the text that closes the block, for example </table>.
  The first occurrence after find_start is the one used, so this one does not
  have to be unique.

Return JSON only:
{{
  "find_start": "opening tag, copied verbatim",
  "find_end": "closing text, copied verbatim",
  "replace": "the complete new block, including its own opening and closing tags"
}}"""

VERIFY_SYSTEM = "You are a strict reviewer of visual document recreations."

VERIFY_USER = """Image 1: source document
Image 2: render before this edit
Image 3: render after this edit
The intended edit goal was:
{plan}
Compare carefully.
Answer:
1. Did the intended change actually happen?
2. Is the new render visibly closer to the source?
3. Did the edit introduce any meaningful regression elsewhere?

A bigger edit usually improves a lot and loses a little. Judge the net result.
"revert" means the page as a whole is now further from the source - not that
you can point at one thing that got worse while the rest improved.

Choose exactly one:
- keep
- revert
- done
Use "done" only when you can find no remaining mismatch worth another round -
placing the two images side by side, a person would call them the same
document. If you can name a largest remaining mismatch in
"next_major_issue", you are not done; choose "keep" or "revert" instead.
Return JSON only:
{{
  "goal_achieved": true,
  "improved": true,
  "regression": false,
  "decision": "keep|revert|done",
  "reason": "short explanation",
  "next_major_issue": "largest remaining mismatch, or empty"
}}"""


# Declared to the model whenever a human is taking part, so operator fields
# never arrive unexplained. Without this, ACTION receives an
# "operator_instruction" it was never told the meaning or precedence of.
OPERATOR_CONTRACT = """A human operator is taking part in this loop, so some of the input you get is written by a person, not by you:
- "operator_instruction" in the plan: a human instruction that REPLACES the plan's own goal. Do what it says instead.
- "operator_note" in the plan: a human comment to take into account WITHOUT discarding the plan.
- a plan with "planned_by": "operator": the human discarded the model's own plan. What the model had proposed is kept under "model_plan" for reference only - do not act on it.
- "operator_region" in the plan: a human marked one area of the page. Confine the edit to it; two extra images zoom in on that area.
- a history line marked (operator: ...): a human comment on an earlier round.
The operator is looking at the same images you are. Prefer their input over your own earlier reasoning, but never over what the current images plainly show. If their input contradicts the images, say so rather than following it blindly."""




# Appended to the system message whenever a stage is thinking. Qwen will
# otherwise reason until it runs out of budget on a dense page -- observed
# spending a whole 32768-token budget on one skeleton_fix and returning no
# answer at all.
THINK_BRIEF = (
    "Think briefly. A few sentences of reasoning is enough. Do not describe the "
    "images back to yourself, do not enumerate every difference you can see, and "
    "do not draft the answer inside your reasoning. Reach the decision, then give "
    "the answer in the required format."
)


def brief_thinking_block(active: bool) -> str:
    """Empty unless this call is thinking and brevity is wanted."""
    return THINK_BRIEF if active else ""

def page_size_block(target: tuple, rendered_height=None, tolerance: float = 0.03) -> str:
    """The page height the recreation is aiming for, and how far off it is.

    The renderer lays out at a fixed width, so the source's aspect ratio fixes
    the height -- but nothing told the model that number, and the two images it
    compares can be at different scales, which hides the error entirely. A page
    a third too tall is a structural fault, so it is stated as one.
    """
    width, height = int(target[0]), int(target[1])
    lines = [
        f"Page size: at this render width the page should be {width} x {height} "
        f"CSS pixels (the source's aspect ratio, 1:{height / width:.3f})."
    ]
    if rendered_height:
        now = round(float(rendered_height))
        off = (now - height) / float(height)
        if abs(off) >= tolerance:
            word = "taller" if off > 0 else "shorter"
            lines.append(
                f"The current render is {width} x {now}, {abs(off) * 100:.0f}% {word} "
                f"than it should be."
            )
            lines.append(
                "That is a structural mismatch, not a detail: find what accounts "
                "for the difference - padding, margins, line height, font sizes, a "
                "row or block that should not be there - and fix that. Do not "
                "scale or stretch anything to hit the number."
            )
        else:
            lines.append(f"The current render is {width} x {now}, which matches.")
    else:
        lines.append("Aim for that height.")
    return "\n".join(lines)

def region_block(region: dict) -> str:
    """Tells ACTION that the last two images are a zoom of a marked area."""
    return (
        "The operator marked one region of the page and wants the edit confined to it.\n"
        f"Region (fraction of the page, from {region.get('panel') or 'the page'}): "
        f"x={region.get('x')}, y={region.get('y')}, "
        f"width={region.get('w')}, height={region.get('h')}.\n"
        "The last two images are that region zoomed in: first from the source, "
        "then from the current render.\n"
        "Fix what those crops show. Leave the rest of the document alone."
    )


def operator_contract_block(active: bool) -> str:
    """Empty unless a human can actually intervene in this run."""
    return OPERATOR_CONTRACT if active else ""


OPERATOR_NOTES_RULE = (
    "Do not treat these notes as a description of what is currently wrong.\n"
    "Judge the images first; the notes only tell you what matters in this document."
)


def notes_block(notes: str) -> str:
    """Operator-supplied context for PLAN and VERIFY. Empty string when unset."""
    notes = (notes or "").strip()
    if not notes:
        return ""
    return f"Notes from the operator about this document:\n{notes}\n\n{OPERATOR_NOTES_RULE}"


FAILED_RULE = (
    "These approaches were already tried and did not work.\n"
    "Do not repeat the same approach. A goal listed here may still be a real\n"
    "problem - if the current images show it, attack it a different way."
)


def failed_block(entries: list[str], limit: int = 8) -> str:
    """Attempts that never landed, kept beyond the short history window.

    Without this, an edit that was reverted five rounds ago falls out of the
    recent history and gets proposed again.
    """
    recent = [e for e in entries if e][-limit:]
    if not recent:
        return ""
    lines = "\n".join(f"- {e}" for e in recent)
    return f"Already tried without success:\n{lines}\n\n{FAILED_RULE}"


def history_block(entries: list[str], limit: int = 3) -> str:
    """Short recent-history block for PLAN. Empty string when there is none."""
    recent = [e for e in entries if e][-limit:]
    if not recent:
        return ""
    lines = "\n".join(f"- {e}" for e in recent)
    return f"Previous attempts:\n{lines}\n\n{HISTORY_RULE}"

