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

