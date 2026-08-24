"""SOURCE -> BOOTSTRAP -> (PLAN -> ACTION -> APPLY -> RENDER -> VERIFY)* -> clone.html"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path

import prompts
from config import Config
from llm import LLMError, QwenClient, image_part, system_message, user_message
from renderer import RendererClient, RendererError
from utils import (
    apply_edits,
    clip,
    apply_section,
    block_markers,
    clean_fragment_output,
    diff_line_count,
    fill_block,
    clean_html_output,
    ensure_dir,
    extract_json,
    html_sanity_check,
    crop_normalized,
    image_size,
    resize_to_width,
    side_by_side,
    target_page_size,
    transcribe_messages,
    truncate,
    write_bytes,
    write_json,
    write_text,
)

LOG = logging.getLogger("docgen.pipeline")

# ACTION receives the whole current HTML. It is never silently truncated: a
# model that is shown a cut-off document returns a cut-off document, and the
# lost content is invisible to every downstream check. Past this size we only
# warn -- a document this dense is the signal to switch ACTION to patch mode.
HTML_WARN_SIZE = 60000

#: Stages that decide something rather than produce something. These reason
#: regardless of llm.thinking; the rest follow it.
JUDGING_STAGES = ("plan", "skeleton_check", "verify")


# The operator model is exactly three states. Everything that needs to know who
# decided reads judged_by(); nothing re-derives it from combinations of optional
# fields. Five copies of that derivation is precisely how they drift apart, and
# every mis-attribution bug in this file's history was one copy disagreeing.
MODEL = "model"
MODEL_AND_OPERATOR = "model+operator"
OPERATOR = "operator"
SOURCES = (MODEL, MODEL_AND_OPERATOR, OPERATOR)


def judged_by(payload: dict | None) -> str:
    """Who decided this plan or verdict. Written at the decision point, not guessed."""
    if not payload:
        return MODEL
    source = payload.get("verified_by") or payload.get("planned_by") or MODEL
    return source if source in SOURCES else MODEL


def touched_by_operator(*payloads: dict | None) -> bool:
    return any(judged_by(p) != MODEL for p in payloads)


def deciding_words(payload: dict | None, model_reason: str) -> str:
    """The reason belonging to whoever decided -- never the discarded one.

    When the operator replaced a verdict, the model's reason explains a verdict
    that was thrown away; presenting it as the operator's is a lie.
    """
    payload = payload or {}
    reason = " ".join(str(model_reason or "").split())
    if reason == "operator verdict":
        reason = ""  # placeholder for a verdict recorded without a reason
    if judged_by(payload) == OPERATOR:
        return str(payload.get("operator_note", "")).strip() or reason
    return reason


class SkipRound(Exception):
    """Raised when the operator chooses to skip the current round."""


@dataclass
class RoundResult:
    index: int
    decision: str
    reason: str = ""
    mode: str = ""
    operator: bool = False
    plan: dict = field(default_factory=dict)
    verify: dict = field(default_factory=dict)
    error: str = ""
    #: Lines the candidate changed. 0 on a round that never produced one. The
    #: whole point of the loop is that this stays above zero.
    changed_lines: int = 0

    #: Outcomes that mean the attempt did not land. An LLM error is not the
    #: plan's fault, and an operator skip is not a failed approach.
    FAILED_DECISIONS = ("revert", "rejected", "noop")

    def failed_line(self) -> str | None:
        """One line for the persistent 'already tried' list, or None."""
        if self.decision not in self.FAILED_DECISIONS:
            return None
        goal = (self.plan.get("goal") or self.plan.get("target") or "edit").strip()
        goal = " ".join(goal.split())[:90]
        if self.decision == "noop":
            why = "the edit produced no change"
        elif self.decision == "rejected":
            why = f"could not be applied ({' '.join(self.error.split())[:70]})"
        else:
            why = " ".join((self.reason or "judged worse").split())[:80]
        return f"{goal} -> {self.decision}: {why}"

    def history_line(self) -> str:
        """One short line for the next PLAN.

        Carries forward what VERIFY concluded, not just the decision: why a
        revert failed (so the next plan does not retry it) and what the verdict
        thought was still wrong. Attributed, because a human's words and the
        model's own carry different weight.
        """
        goal = (self.plan.get("goal") or self.plan.get("target") or "edit").strip()
        goal = goal.replace("\n", " ")
        if len(goal) > 90:
            goal = goal[:90] + "..."
        line = f"Round {self.index}: {goal} -> {self.decision}"

        def short(value) -> str:
            return " ".join(str(value).split())[:80]

        judged = judged_by(self.verify)
        by_operator = judged == OPERATOR
        note = str(self.verify.get("operator_note", "")).strip()

        bits: list[str] = []
        own = deciding_words(self.verify, self.reason)
        if own and (self.decision == "revert" or by_operator):
            # Always carry why a revert failed; on a keep, carry it when a
            # person bothered to type one.
            bits.append(f"why: {short(own)}")

        remaining = str(self.verify.get("next_major_issue", "")).strip()
        if remaining:
            bits.append(f"next: {short(remaining)}")

        # A note attached while the model's verdict stood is a second voice.
        if note and not by_operator:
            bits.append(f"operator: {short(note)}")

        # Tag whenever a person was involved, even with nothing else to say:
        # "a human accepted this" is itself signal for the next plan. A pure
        # model round with nothing to add stays unadorned.
        if bits:
            line += f" [{judged}] " + "; ".join(bits)
        elif judged != MODEL:
            line += f" [{judged}]"
        return line


class Pipeline:
    def __init__(
        self,
        cfg: Config,
        out_dir: str | Path,
        notes: str = "",
        interactive: bool = False,
        verify_mode: str | None = None,
        prompter=None,
    ) -> None:
        # Asking to intervene implies intervening at VERIFY too, unless the
        # caller names a mode. Keeps the library and the CLI in agreement.
        if verify_mode is None:
            verify_mode = "both" if interactive else "model"
        if verify_mode not in ("model", "both"):
            raise ValueError(f"verify_mode must be model or both, got {verify_mode!r}")
        self.cfg = cfg
        self.notes = (notes or "").strip()
        # What the run was started with. The UI may override these mid-run, so
        # interactive / verify_mode are properties read once per round.
        self._base_interactive = interactive
        self._base_verify_mode = verify_mode
        # Anything with .ask(prompt, context) -> str. None means the terminal.
        self.prompter = prompter
        if hasattr(prompter, "announce_defaults"):
            prompter.announce_defaults(verify_mode, interactive)
        # Region the operator marked alongside their last answer, if any.
        self._last_region: dict | None = None
        self.interventions = 0
        self.llm = QwenClient(cfg.llm)
        self.renderer = RendererClient(
            base_url=cfg.renderer.url,
            timeout=cfg.renderer.timeout,
            device_scale=cfg.renderer.device_scale,
        )
        self.out = ensure_dir(out_dir)
        self.rounds_dir = ensure_dir(self.out / "rounds")
        self.max_side = cfg.llm.image_max_side
        # The page size the recreation is aiming for, in CSS pixels, and the
        # height the last render actually produced. Both are facts the model
        # cannot work out from images that may be at different scales.
        self.target_page: tuple[int, int] | None = None
        self.render_height: float | None = None
        self._calls = 0

    # ---------------------------------------------------------------- render

    def render(self, html: str) -> tuple[bytes, dict]:
        png, metrics = self.renderer.probe(
            html,
            width=self.cfg.renderer.width,
            wait_ms=self.cfg.renderer.wait_ms,
        )
        # Every render passes through here, so this is where the current page
        # height is learned. The next prompt states it against the target.
        page = metrics.get("page") if isinstance(metrics, dict) else None
        height = (page or {}).get("height") or (metrics or {}).get("scrollHeight")
        self.render_height = float(height) if height else None
        return png, metrics

    def _img(self, path_or_bytes):
        return image_part(path_or_bytes, max_side=self.max_side)

    def _ref(self, path_or_bytes):
        """The source at the render's own width, so proportions are comparable.

        Sent wherever the question is geometric. A scan is several times wider
        than the render in pixels; asking whether one is taller than the other
        across that scale difference is asking the model to do arithmetic on
        two images instead of looking at them.
        """
        width = int(self.cfg.renderer.width * max(self.cfg.renderer.device_scale, 1.0))
        return image_part(resize_to_width(path_or_bytes, width), max_side=self.max_side)

    # ------------------------------------------------------------ llm calls

    def thinking_for(self, stage: str) -> bool:
        """Whether this stage reasons before answering.

        The judging stages always do: choosing what is wrong, and deciding
        whether an edit helped, is the whole of their work. Whether the
        generating stages should is a real trade -- reasoning tokens come out of
        the same max_tokens budget as the HTML, so a document large enough to
        nearly fill that budget can be pushed over it by thinking, and the round
        is rejected as truncated. "all" takes that trade; "judging" does not.
        """
        if self.cfg.llm.thinking == "all":
            return True
        return stage.split(":", 1)[0] in JUDGING_STAGES

    def _budget_hint(self) -> str:
        """Named only when thinking is a plausible part of why output ran out."""
        if self.cfg.llm.thinking != "all":
            return ""
        return (
            " (llm.thinking=all, so reasoning shares that budget with the HTML; "
            "thinking=\"judging\" gives generating stages the whole of it)"
        )

    def _chat(self, messages, stage: str):
        """Every model call goes through here.

        Which makes it the one place that can guarantee two things for every
        stage: the configured thinking policy, and that the page-size fact is
        present. A stage that silently lacked the second is the bug this fixes.
        """
        if self.target_page:
            messages = self._with_page_size(messages)
        thinking = self.thinking_for(stage)
        if thinking and self.cfg.llm.thinking_brief:
            messages = self._with_brief_thinking(messages)

        self._calls += 1
        path = ensure_dir(self.out / "llm") / f"{self._calls:03d}_{stage.replace(':', '-')}.txt"
        prompt = transcribe_messages(messages)
        header = f"call {self._calls}  stage={stage}  thinking={thinking}"
        write_text(path, f"=== {header} ===\n\n{prompt}\n")
        self._log_block(stage, f"prompt ({len(prompt)} chars) -> {path.name}", prompt)

        try:
            resp = self.llm.chat(messages, thinking=thinking, stage=stage)
        except LLMError as exc:
            with open(path, "a", encoding="utf-8") as fh:
                fh.write(f"\n=== FAILED ===\n{exc}\n")
                # A call that spent its whole budget thinking produced no answer
                # but plenty of reasoning, and that reasoning is the only record
                # of what it was doing for those minutes.
                reasoning = getattr(exc, "reasoning", "")
                if reasoning:
                    fh.write(
                        f"\n=== reasoning of the failed call ({len(reasoning)} chars) "
                        f"===\n{reasoning}\n"
                    )
            if reasoning:
                self._log_block(
                    stage, f"reasoning of the FAILED call ({len(reasoning)} chars)", reasoning
                )
            raise

        with open(path, "a", encoding="utf-8") as fh:
            if resp.reasoning:
                fh.write(f"\n=== reasoning ({len(resp.reasoning)} chars) ===\n{resp.reasoning}\n")
            fh.write(
                f"\n=== response  finish={resp.finish_reason}  "
                f"{len(resp.content)} chars ===\n{resp.content}\n"
            )
        if resp.reasoning:
            self._log_block(stage, f"reasoning ({len(resp.reasoning)} chars)", resp.reasoning)
        self._log_block(
            stage,
            f"response ({len(resp.content)} chars, finish={resp.finish_reason or '?'})",
            resp.content,
        )
        return resp

    def _log_block(self, stage: str, title: str, body: str) -> None:
        """One labelled block of a call, on the terminal and in run.log.

        The transcripts under llm/ hold everything, but a file nobody opens is
        not a log. What is actually read is this, so it goes to the same place
        as the rest of the run's output, clipped to stay readable.
        """
        if not self.cfg.llm.log_calls:
            return
        LOG.info(
            "[%s] ---- %s ----\n%s", stage, title, clip(body, self.cfg.llm.log_chars)
        )

    def _with_brief_thinking(self, messages: list) -> list:
        """Ask for short reasoning, on the system message where it belongs."""
        block = prompts.brief_thinking_block(True)
        out = [dict(m) for m in messages]
        for message in out:
            if message.get("role") != "system":
                continue
            content = message.get("content")
            if isinstance(content, str):
                message["content"] = f"{content}\n\n{block}"
                return out
        return [{"role": "system", "content": block}] + out

    def _with_page_size(self, messages: list) -> list:
        """Append the page-size fact to the last text part of the last user turn."""
        block = prompts.page_size_block(self.target_page, self.render_height)
        out = [dict(m) for m in messages]
        for message in reversed(out):
            if message.get("role") != "user":
                continue
            content = message.get("content")
            if isinstance(content, str):
                message["content"] = f"{content}\n\n{block}"
                return out
            parts = list(content or [])
            for index in range(len(parts) - 1, -1, -1):
                part = parts[index]
                if isinstance(part, dict) and part.get("type") == "text":
                    part = dict(part)
                    part["text"] = f"{part.get('text', '')}\n\n{block}"
                    parts[index] = part
                    message["content"] = parts
                    return out
            message["content"] = parts + [{"type": "text", "text": block}]
            return out
        return out

    # ------------------------------------------------------------- bootstrap

    def bootstrap(self, source_png: Path) -> tuple[str, bytes]:
        """The first draft, and the only stage that starts from nothing.

        Staged by default. One call cannot get a dense page both structurally
        and typographically right, and the loop afterwards fixes one thing a
        round, so a draft with the wrong layout is never caught up with.
        """
        # Fixed for the whole run: the renderer always lays out at one width, so
        # the source's aspect ratio is the height every stage is aiming for.
        self.target_page = target_page_size(source_png, self.cfg.renderer.width)
        LOG.info(
            "TARGET: the page should render %dx%d CSS px (source aspect 1:%.3f)",
            self.target_page[0],
            self.target_page[1],
            self.target_page[1] / self.target_page[0],
        )

        if self.cfg.bootstrap.staged:
            html, png, metrics, stages = self._bootstrap_staged(source_png)
        else:
            html, png, metrics = self._bootstrap_single(source_png)
            stages = [{"step": "single", "chars": len(html)}]

        write_text(self.rounds_dir / "bootstrap.html", html)
        write_bytes(self.rounds_dir / "bootstrap.png", png)
        write_json(self.rounds_dir / "bootstrap_metrics.json", metrics)
        write_json(self.rounds_dir / "bootstrap_stages.json", {"stages": stages})
        LOG.info("BOOTSTRAP: %d chars html, render %sx%s", len(html), *image_size(png))
        return html, png

    def _rough(self, path_or_bytes):
        """The page with the detail thrown away, for the structure-only steps.

        Normalised by width, not by longest side. max_side caps the longest
        side, so a 1:1.41 scan and a 1:1.75 render came out at different widths
        and the layout comparison was made across a scale difference -- exactly
        the question those steps exist to answer. The max_side here is only a
        guard against a runaway-tall render.
        """
        rough = min(self.cfg.bootstrap.rough_max_side, self.cfg.renderer.width)
        return image_part(resize_to_width(path_or_bytes, rough), max_side=4 * rough)

    def _stage_render(self, name: str, html: str) -> tuple[bytes, dict]:
        """Render one bootstrap stage, keeping both halves for inspection."""
        write_text(self.rounds_dir / f"{name}.html", html)
        png, metrics = self.render(html)
        write_bytes(self.rounds_dir / f"{name}.png", png)
        return png, metrics

    def _whole_document(self, resp, label: str) -> str:
        """HTML from a generation that has to be a complete, valid document."""
        if resp.finish_reason == "length":
            raise RuntimeError(
                f"{label} hit max_tokens={self.cfg.llm.max_tokens}; the HTML is "
                f"truncated{self._budget_hint()}"
            )
        html = clean_html_output(resp.content)
        ok, reason = html_sanity_check(html)
        if not ok:
            raise RuntimeError(f"{label} produced unusable HTML: {reason}")
        return html

    def _bootstrap_single(self, source_png: Path) -> tuple[str, bytes, dict]:
        """The whole page in one call. Thinking OFF."""
        LOG.info("BOOTSTRAP: generating initial HTML in one pass")
        resp = self._chat(
            [
                system_message(prompts.BOOTSTRAP_SYSTEM),
                user_message(
                    prompts.BOOTSTRAP_USER.format(width=self.cfg.renderer.width),
                    self._img(source_png),
                ),
            ],
            stage="bootstrap",
        )
        write_text(self.rounds_dir / "bootstrap_raw.txt", resp.content)
        html = self._whole_document(resp, "BOOTSTRAP")
        png, metrics = self.render(html)
        return html, png, metrics

    # ------------------------------------------------- staged bootstrap

    def _bootstrap_staged(self, source_png: Path) -> tuple[str, bytes, dict, list]:
        """Structure first, checked by eye, then one block at a time."""
        stages: list[dict] = []

        html, png, metrics = self._skeleton(source_png, stages)
        html, png, metrics = self._skeleton_check(source_png, html, png, metrics, stages)
        html, png, metrics = self._fill_blocks(source_png, html, png, metrics, stages)
        return html, png, metrics, stages

    def _skeleton(self, source_png: Path, stages: list) -> tuple[str, bytes, dict]:
        """Step 1. Layout only, from a source too small to read. Thinking OFF."""
        bcfg = self.cfg.bootstrap
        LOG.info("BOOTSTRAP 1/3: structure, from a %dpx view of the page", bcfg.rough_max_side)
        resp = self._chat(
            [
                system_message(prompts.SKELETON_SYSTEM),
                user_message(
                    prompts.SKELETON_USER.format(
                        width=self.cfg.renderer.width, max_blocks=bcfg.max_blocks
                    ),
                    self._rough(source_png),
                ),
            ],
            stage="skeleton",
        )
        write_text(self.rounds_dir / "bootstrap_skeleton_raw.txt", resp.content)
        html = self._whole_document(resp, "SKELETON")
        png, metrics = self._stage_render("bootstrap_skeleton", html)
        blocks = len(block_markers(html))
        LOG.info("BOOTSTRAP: skeleton is %d chars and marks %d block(s)", len(html), blocks)
        stages.append({"step": "skeleton", "chars": len(html), "blocks": blocks})
        return html, png, metrics

    def _skeleton_check(
        self, source_png: Path, html: str, png: bytes, metrics: dict, stages: list
    ) -> tuple[str, bytes, dict]:
        """Step 2. One look at the layout, and at most one correction.

        Both images go in shrunk: at that size the text is gone, which is the
        point - "is this the same page" is answerable, "is this the same font"
        is not, and only the first question is being asked yet.
        """
        LOG.info("BOOTSTRAP 2/3: comparing the layout at a glance")
        check: dict = {}
        try:
            resp = self._chat(
                [
                    system_message(prompts.SKELETON_CHECK_SYSTEM),
                    user_message(
                        prompts.SKELETON_CHECK_USER,
                        self._rough(source_png),
                        self._rough(png),
                    ),
                ],
                stage="skeleton_check",
            )
            check = extract_json(resp.content)
        except (LLMError, ValueError) as exc:
            LOG.warning("BOOTSTRAP: the layout check failed (%s); keeping the skeleton", exc)
        write_json(self.rounds_dir / "bootstrap_skeleton_check.json", check)

        problems = [str(p).strip() for p in (check.get("problems") or []) if str(p).strip()]
        goal = str(check.get("goal", "")).strip()
        # A check that could not be read defaults to "matches": with no problems
        # named there is nothing to correct, and inventing one wastes a call.
        if bool(check.get("matches", True)) or not problems:
            LOG.info("BOOTSTRAP: the layout was judged close enough to fill in")
            stages.append({"step": "layout_check", "matches": True, "problems": problems})
            return html, png, metrics

        LOG.info("BOOTSTRAP: correcting the layout: %s", truncate("; ".join(problems), 160))
        stages.append({"step": "layout_check", "matches": False, "problems": problems,
                       "goal": goal})
        try:
            resp = self._chat(
                [
                    system_message(prompts.SKELETON_SYSTEM),
                    user_message(
                        prompts.SKELETON_FIX_USER.format(
                            problems="\n".join(f"- {p}" for p in problems),
                            goal=goal or "make the layout match the source",
                            html=html,
                        ),
                        self._rough(source_png),
                        self._rough(png),
                    ),
                ],
                stage="skeleton_fix",
            )
            fixed = self._whole_document(resp, "SKELETON FIX")
            png, metrics = self._stage_render("bootstrap_rough", fixed)
        except (LLMError, RendererError, RuntimeError) as exc:
            LOG.warning("BOOTSTRAP: the layout fix did not land (%s); keeping the skeleton", exc)
            stages.append({"step": "layout_fix", "landed": False, "error": str(exc)})
            return html, png, metrics

        blocks = len(block_markers(fixed))
        LOG.info("BOOTSTRAP: layout corrected, %d chars, %d block(s)", len(fixed), blocks)
        stages.append({"step": "layout_fix", "landed": True, "chars": len(fixed),
                       "blocks": blocks})
        return fixed, png, metrics

    def _fill_blocks(
        self, source_png: Path, html: str, png: bytes, metrics: dict, stages: list
    ) -> tuple[str, bytes, dict]:
        """Step 3. Detail, one marked block at a time. Thinking OFF.

        Sequential rather than parallel: each block is written against a render
        of the document as the previous blocks left it. A block that fails is
        left as the skeleton drew it -- the PLAN/ACTION loop can still reach it.
        """
        markers = block_markers(html)
        if not markers:
            LOG.warning(
                "BOOTSTRAP: the skeleton marked no blocks (no data-block attribute), "
                "so there is nothing to fill; the loop starts from the skeleton"
            )
            stages.append({"step": "fill", "blocks": 0, "filled": 0})
            return html, png, metrics

        limit = self.cfg.bootstrap.max_blocks
        if len(markers) > limit:
            LOG.warning(
                "BOOTSTRAP: the skeleton marked %d blocks, filling the first %d "
                "(bootstrap.max_blocks); the rest stay as drawn",
                len(markers),
                limit,
            )
            markers = markers[:limit]

        LOG.info("BOOTSTRAP 3/3: filling %d block(s) in reading order", len(markers))
        filled = 0
        for number, marker in enumerate(markers, 1):
            label = f"block {marker['id']}" + (f" ({marker['role']})" if marker["role"] else "")
            try:
                resp = self._chat(
                    [
                        system_message(prompts.FILL_SYSTEM),
                        user_message(
                            prompts.FILL_USER.format(
                                block_id=marker["id"],
                                role=marker["role"] or "this block",
                                html=html,
                            ),
                            self._img(source_png),
                            self._img(png),
                        ),
                    ],
                    stage=f"fill:{marker['id']}",
                )
                if resp.finish_reason == "length":
                    raise ValueError(
                        f"hit max_tokens={self.cfg.llm.max_tokens}, the block is "
                        f"truncated{self._budget_hint()}"
                    )
                fragment = clean_fragment_output(resp.content, marker["tag"])
                candidate, span = fill_block(html, marker, fragment)
                ok, reason = html_sanity_check(candidate)
                if not ok:
                    raise ValueError(f"the document would be broken: {reason}")
                png, metrics = self._stage_render(f"bootstrap_fill_{number:02d}", candidate)
            except (LLMError, RendererError, ValueError) as exc:
                LOG.warning("BOOTSTRAP: %s left as drawn: %s", label, exc)
                stages.append({"step": "fill", "block": marker["id"], "landed": False,
                               "error": str(exc)})
                continue
            html = candidate
            filled += 1
            LOG.info("BOOTSTRAP: %s filled (%s)", label, span)
            stages.append({"step": "fill", "block": marker["id"], "landed": True,
                           "role": marker["role"], "span": span})

        LOG.info("BOOTSTRAP: %d of %d block(s) filled", filled, len(markers))
        return html, png, metrics

    # ------------------------------------------------------------------ plan

    def plan(
        self,
        source_png: Path,
        current_png: bytes,
        history: list[str],
        failed: list[str] | None = None,
    ) -> dict:
        """PLAN. Thinking ON. Images are the primary evidence."""
        text = prompts.PLAN_USER
        contract = prompts.operator_contract_block(self.operator_active)
        if contract:
            text = f"{text}\n\n{contract}"
        notes = prompts.notes_block(self.notes)
        if notes:
            text = f"{text}\n\n{notes}"
        hist = prompts.history_block(history)
        if hist:
            text = f"{text}\n\n{hist}"
        dead_ends = prompts.failed_block(failed or [])
        if dead_ends:
            text = f"{text}\n\n{dead_ends}"

        messages = [
            system_message(prompts.PLAN_SYSTEM),
            user_message(
                text,
                self._ref(source_png),
                self._img(current_png),
            ),
        ]
        resp = self._chat(messages, stage="plan")
        plan = extract_json(resp.content)
        LOG.info(
            "PLAN: scope=%s target=%s",
            plan.get("scope", "?"),
            truncate(str(plan.get("target", "?")), 80),
        )
        return plan

    # ---------------------------------------------------------------- action

    @property
    def interactive(self) -> bool:
        override = getattr(self.prompter, "plan_interactive_override", None)
        return self._base_interactive if override is None else bool(override)

    @property
    def verify_mode(self) -> str:
        return getattr(self.prompter, "verify_mode_override", None) or self._base_verify_mode

    @property
    def operator_active(self) -> bool:
        """Gates the contract block: can a person intervene in this run at all?"""
        return (
            self._base_interactive
            or self._base_verify_mode != "model"
            or self.interactive
            or self.verify_mode != "model"
        )

    @staticmethod
    def action_mode(plan: dict, html_size: int = 0) -> str:
        """PLAN's scope is the mode switch, with one size-driven override.

        local   -> patch:   exact search/replace, response size tracks the edit
        section -> section: one block rebuilt, response size tracks the block
        global  -> rewrite: full document, when the page layout itself is wrong

        Without the middle mode a structural problem has nowhere to go: patch
        can only nudge properties, and rewrite has to re-emit the whole
        document. That is how a loop ends up changing one declaration a round.

        An unrecognised scope lands on section as well - it is the mode that can
        still make a real change without betting the round on max_tokens.
        """
        scope = str(plan.get("scope", "")).strip().lower()
        if scope.startswith("local"):
            return "patch"
        if scope.startswith("global"):
            # A rewrite has to emit the whole document. Past this size that is a
            # coin flip against max_tokens, and a truncated response costs the
            # round; rebuilding the worst block instead actually lands.
            if html_size > HTML_WARN_SIZE:
                LOG.warning(
                    "ACTION: plan is global but the document is %d chars; "
                    "rebuilding one block instead of risking a truncated rewrite",
                    html_size,
                )
                return "section"
            return "rewrite"
        if not scope.startswith("section"):
            LOG.warning("ACTION: unrecognised plan scope %r; treating it as section", scope)
        return "section"

    ACTION_TEMPLATES = {
        "patch": "ACTION_PATCH_USER",
        "section": "ACTION_SECTION_USER",
        "rewrite": "ACTION_REWRITE_USER",
    }

    def action(self, plan: dict, current_html: str, source_png: Path, current_png: bytes):
        """ACTION. Thinking OFF. Returns (mode, LLMResponse)."""
        mode = self.action_mode(plan, len(current_html))
        template = getattr(prompts, self.ACTION_TEMPLATES[mode])
        text = template.format(
            plan=json.dumps(plan, ensure_ascii=False, indent=2),
            html=current_html,
        )
        contract = prompts.operator_contract_block(self.operator_active)
        if contract:
            text = f"{text}\n\n{contract}"

        parts = [self._ref(source_png), self._img(current_png)]
        region = plan.get("operator_region")
        if isinstance(region, dict):
            # Zoomed crops of the marked area: the same normalised rect applies
            # to the source and the render even at different pixel sizes.
            try:
                parts.append(self._img(crop_normalized(source_png, region)))
                parts.append(self._img(crop_normalized(current_png, region)))
                text = f"{text}\n\n{prompts.region_block(region)}"
                LOG.info("ACTION: operator region attached as crops %s", region)
            except (OSError, ValueError) as exc:
                LOG.warning("ACTION: could not crop the operator region: %s", exc)
        messages = [
            system_message(prompts.ACTION_SYSTEM),
            user_message(text, *parts),
        ]
        LOG.info("ACTION: mode=%s (scope=%s)", mode, plan.get("scope", "?"))
        return mode, self._chat(messages, stage=f"action:{mode}")

    # ----------------------------------------------------------------- apply

    @staticmethod
    def apply(action_raw: str, previous_html: str, mode: str = "rewrite") -> str:
        """APPLY (Python). patch -> exact edits, section -> splice, rewrite -> fence removal."""
        if mode == "patch":
            payload = extract_json(action_raw)
            html, applied = apply_edits(previous_html, payload.get("edits"))
            LOG.info("APPLY: %d patch edit(s): %s", len(applied), "; ".join(applied)[:300])
        elif mode == "section":
            payload = extract_json(action_raw)
            html, span = apply_section(previous_html, payload)
            LOG.info("APPLY: rebuilt block %r (%s)", str(payload.get("find_start"))[:60], span)
        else:
            html = clean_html_output(action_raw)

        ok, reason = html_sanity_check(html)
        if not ok:
            raise ValueError(f"candidate rejected: {reason}")
        # A candidate that collapsed to a fraction of the previous document is
        # almost always a truncated generation, not a real edit.
        if previous_html and len(html) < 0.4 * len(previous_html):
            raise ValueError(
                f"candidate rejected: shrank from {len(previous_html)} to {len(html)} chars"
            )
        return html

    # ---------------------------------------------------------------- verify

    def verify(self, plan: dict, source_png: Path, before_png: bytes, candidate_png: bytes) -> dict:
        """VERIFY. Thinking ON. Three images: source, before, after."""
        text = prompts.VERIFY_USER.format(plan=json.dumps(plan, ensure_ascii=False, indent=2))
        contract = prompts.operator_contract_block(self.operator_active)
        if contract:
            text = f"{text}\n\n{contract}"
        notes = prompts.notes_block(self.notes)
        if notes:
            text = f"{text}\n\n{notes}"
        messages = [
            system_message(prompts.VERIFY_SYSTEM),
            user_message(
                text,
                self._ref(source_png),
                self._img(before_png),
                self._img(candidate_png),
            ),
        ]
        resp = self._chat(messages, stage="verify")
        verdict = extract_json(resp.content)

        decision = str(verdict.get("decision", "")).strip().lower()
        if decision not in ("keep", "revert", "done"):
            LOG.warning("VERIFY returned unknown decision %r; treating as revert", decision)
            decision = "revert"
            verdict["decision"] = decision
            verdict.setdefault("reason", "unparsable decision")
        # "done" while still naming a largest remaining mismatch is a verdict
        # arguing with itself, and it ends the run early. The named issue is the
        # more specific half of the answer, so keep the edit and keep going.
        remaining = str(verdict.get("next_major_issue", "")).strip()
        if decision == "done" and remaining:
            LOG.warning(
                "VERIFY said done but still names a remaining issue (%s); continuing",
                truncate(remaining, 80),
            )
            decision = "keep"
            verdict["decision"] = decision
            verdict["downgraded_from"] = "done"
        LOG.info("VERIFY: %s (%s)", decision, truncate(str(verdict.get("reason", "")), 100))
        return verdict

    # -------------------------------------------------------- operator input

    def _input(self, prompt: str, context: dict | None = None) -> str:
        """Always reads, regardless of the interactive flag."""
        self._last_region = None
        if self.prompter is not None:
            text = self.prompter.ask(prompt, context or {})
            self._last_region = getattr(self.prompter, "last_region", None)
            return text.strip()
        try:
            return input(prompt).strip()
        except (EOFError, KeyboardInterrupt):
            return ""

    def _ask(self, prompt: str, context: dict | None = None) -> str:
        if not self.interactive:
            return ""
        return self._input(prompt, context)

    PLAN_PROMPT = (
        "개입 (Enter=① Qwen 계획대로 / a <의견>=② 참고로 첨부 / "
        "x <지시>=③ Qwen 계획 버리고 내 지시만 / s=건너뛰기): "
    )

    def review_plan(self, plan: dict, context: dict | None = None) -> dict:
        """Let the operator amend or replace the plan before ACTION acts on it."""
        plan.setdefault("planned_by", MODEL)
        if not self.interactive:
            return plan
        print("\n--- PLAN ---")
        print(json.dumps(plan, ensure_ascii=False, indent=2))

        ctx = {
            "stage": "PLAN",
            "round": (context or {}).get("round", ""),
            "title": f"PLAN — 라운드 {(context or {}).get('round', '?')}",
            "data": plan,
            "image": (context or {}).get("image"),
            "image_panels": (context or {}).get("image_panels") or [],
            "image2": (context or {}).get("image2"),
            "image2_label": (context or {}).get("image2_label", ""),
            "text": True,
            "enter_value": "a @text",
            "choices": [
                # The three choices are numbered so the screen shows three
                # options, not a row of buttons. Skip is not one of them.
                {"label": "① Qwen 계획대로", "value": "", "style": "primary"},
                {"label": "② 참고로 첨부 (계획 유지)", "value": "a @text"},
                {"label": "③ Qwen 계획 버리고 내 지시만", "value": "x @text"},
                {"label": "라운드 건너뛰기", "value": "s", "style": "warn"},
            ],
        }
        for attempt in range(3):
            answer = self._ask(self.PLAN_PROMPT, ctx)
            if not answer:
                return plan
            head, _, rest = answer.partition(" ")
            head = head.lower()
            rest = rest.strip()

            if head == "s":
                raise SkipRound("operator skipped the round")
            if head in ("keep", "revert", "done"):
                # The two prompts look alike; a VERIFY answer typed here used to
                # be injected into the plan as a nonsense instruction.
                print(f"'{answer}' 는 VERIFY 판정어입니다. 여기는 PLAN 단계입니다.")
                continue
            if head in ("a", "x"):
                if not rest:
                    print(f"'{head}' 뒤에 내용을 함께 적어주세요.")
                    continue
                if head == "x":
                    # The model's plan is discarded outright. It is kept under
                    # model_plan so the record still shows what Qwen proposed.
                    model_plan = {
                        k: v for k, v in plan.items()
                        if k not in ("planned_by", "operator_instruction",
                                     "operator_note", "operator_region", "model_plan")
                    }
                    scope = plan.get("scope", "local")
                    plan.clear()
                    plan.update(
                        {
                            "scope": scope,
                            "target": "operator instruction",
                            "problem": rest,
                            "cause": "",
                            "goal": rest,
                            "operator_instruction": rest,
                            "model_plan": model_plan,
                            "planned_by": OPERATOR,
                        }
                    )
                    LOG.info("PLAN: operator discarded the model plan")
                else:
                    plan["operator_note"] = rest
                    plan["planned_by"] = MODEL_AND_OPERATOR
                    LOG.info("PLAN: operator attached a note")
                if self._last_region:
                    plan["operator_region"] = self._last_region
                    LOG.info("PLAN: operator marked a region %s", self._last_region)
                self.interventions += 1
                return plan
            print("a(② 참고 첨부) / x(③ 계획 버리고 내 지시만) / s(건너뛰기) 중에서 골라주세요.")
            if attempt == 2:
                print("입력을 이해하지 못했습니다. 계획을 그대로 수락합니다.")
        return plan

    VERIFY_PROMPT = (
        "개입 (Enter=① Qwen 판정대로 / a <의견>=② 참고로 첨부 / "
        "keep|revert|done=③ Qwen 판정 버리고 내 판정): "
    )

    def review_verify(self, verdict: dict, context: dict | None = None) -> dict:
        """Let the operator attach an opinion to, or overrule, the model's verdict."""
        if not self.interactive:
            return verdict
        print("\n--- VERIFY ---")
        print(json.dumps(verdict, ensure_ascii=False, indent=2))

        ctx = {
            "stage": "VERIFY",
            "round": (context or {}).get("round", ""),
            "title": f"VERIFY — 라운드 {(context or {}).get('round', '?')}"
                     f" · 모델 판정: {verdict.get('decision', '?')}",
            "data": verdict,
            "image": (context or {}).get("image"),
            "image_panels": (context or {}).get("image_panels") or [],
            "image2": (context or {}).get("image2"),
            "image2_label": (context or {}).get("image2_label", ""),
            "text": True,
            "choices": [
                # Same three; the third needs a verdict, so it is three buttons
                # sharing one number rather than three separate choices.
                {"label": "① Qwen 판정대로", "value": "", "style": "primary"},
                {"label": "② 참고로 첨부 (판정 유지)", "value": "a @text"},
                {"label": "③ 내 판정: keep", "value": "keep"},
                {"label": "③ 내 판정: revert", "value": "revert", "style": "warn"},
                {"label": "③ 내 판정: done", "value": "done"},
            ],
        }
        for attempt in range(3):
            answer = self._ask(self.VERIFY_PROMPT, ctx)
            if not answer:
                return verdict
            head, _, rest = answer.partition(" ")
            head = head.lower()
            rest = rest.strip()

            if head == "a":
                if not rest:
                    print("'a' 뒤에 의견을 함께 적어주세요.")
                    continue
                # The decision stands; the comment travels to the next PLAN.
                verdict["operator_note"] = rest
                if self._last_region:
                    verdict["operator_region"] = self._last_region
                verdict["verified_by"] = MODEL_AND_OPERATOR
                self.interventions += 1
                LOG.info("VERIFY: operator attached a note, decision unchanged")
                return verdict
            if head in ("keep", "revert", "done"):
                if head == verdict.get("decision"):
                    return verdict  # agreeing is not an intervention
                verdict["model_decision"] = verdict.get("decision")
                verdict["operator_override"] = head
                verdict["decision"] = head
                # The decision is the operator's now. Mirrors planned_by on the
                # PLAN side: the model's verdict is kept only as a record.
                verdict["verified_by"] = OPERATOR
                if rest:
                    verdict["operator_note"] = rest
                self.interventions += 1
                LOG.info("VERIFY: operator overrode %s -> %s", verdict["model_decision"], head)
                return verdict
            print("a(② 참고 첨부) / keep / revert / done(③ 내 판정) 중에서 골라주세요.")
            if attempt == 2:
                print("입력을 이해하지 못했습니다. 모델 판정을 그대로 둡니다.")
        return verdict

    # ------------------------------------------------------------------ loop

    def build(self, source: str | Path) -> dict:
        source_png = Path(source)
        if not source_png.exists():
            raise FileNotFoundError(f"source image not found: {source_png}")

        current_html, current_png = self.bootstrap(source_png)
        history: list[str] = []
        # Persists for the whole run, unlike the 3-entry history window.
        failed: list[str] = []
        results: list[RoundResult] = []

        def record(result: RoundResult) -> None:
            results.append(result)
            line = result.failed_line()
            if line and line not in failed:
                failed.append(line)
        last_verify: dict = {}
        stop_reason = "max_rounds"

        for index in range(1, self.cfg.loop.max_rounds + 1):
            rdir = ensure_dir(self.rounds_dir / f"r{index:02d}")
            LOG.info("=== round %d/%d ===", index, self.cfg.loop.max_rounds)

            write_text(rdir / "before.html", current_html)
            write_bytes(rdir / "before.png", current_png)

            # PLAN
            try:
                plan = self.plan(source_png, current_png, history, failed)
                if self.interactive:
                    # Source next to the current render: what PLAN itself saw.
                    plan_view, plan_panels = side_by_side(
                        [("1. SOURCE", str(source_png)), ("2. CURRENT RENDER", current_png)],
                        rdir / "plan_view.png",
                    )
                    plan = self.review_plan(
                        plan,
                        {"round": index, "image": str(plan_view), "image_panels": plan_panels},
                    )
                else:
                    plan = self.review_plan(plan)
            except SkipRound as exc:
                LOG.info("round %d: %s", index, exc)
                record(RoundResult(index, "skipped", operator=True))
                history.append(f"Round {index}: skipped by the operator")
                continue
            except (LLMError, ValueError) as exc:
                LOG.error("round %d: PLAN failed: %s", index, exc)
                record(RoundResult(index, "error", error=f"plan: {exc}"))
                write_json(rdir / "error.json", {"stage": "plan", "error": str(exc)})
                continue
            write_json(rdir / "plan.json", plan)

            # ACTION
            try:
                mode, action_resp = self.action(plan, current_html, source_png, current_png)
                action_raw = action_resp.content
            except LLMError as exc:
                LOG.error("round %d: ACTION failed: %s", index, exc)
                record(RoundResult(index, "error", plan=plan, error=f"action: {exc}", operator=touched_by_operator(plan)))
                write_json(rdir / "error.json", {"stage": "action", "error": str(exc)})
                continue
            write_text(rdir / "action_raw.txt", action_raw)
            if mode in ("patch", "section"):
                try:
                    write_json(rdir / "patch.json", extract_json(action_raw))
                except ValueError:
                    pass  # apply() reports the parse failure with a usable message

            # APPLY
            try:
                if action_resp.finish_reason == "length":
                    # The generation was cut off at max_tokens. Whatever HTML it
                    # contains is incomplete by construction, so never adopt it.
                    raise ValueError(
                        f"candidate rejected: ACTION hit max_tokens "
                        f"({self.cfg.llm.max_tokens}); the rewrite is truncated"
                        f"{self._budget_hint()}"
                    )
                candidate_html = self.apply(action_raw, current_html, mode=mode)
            except ValueError as exc:
                LOG.warning("round %d: APPLY rejected the candidate: %s", index, exc)
                record(RoundResult(index, "rejected", mode=mode, plan=plan, error=str(exc), operator=touched_by_operator(plan)))
                history.append(f"Round {index}: {plan.get('goal', 'edit')} -> rejected (invalid HTML)")
                write_json(rdir / "error.json", {"stage": "apply", "error": str(exc)})
                continue
            write_text(rdir / "candidate.html", candidate_html)

            changed_lines = diff_line_count(current_html, candidate_html)
            LOG.info(
                "round %d: candidate changes %d line(s), %+d chars",
                index,
                changed_lines,
                len(candidate_html) - len(current_html),
            )

            if candidate_html.strip() == current_html.strip():
                LOG.warning("round %d: candidate is identical to current HTML; skipping", index)
                record(RoundResult(index, "noop", mode=mode, plan=plan, operator=touched_by_operator(plan)))
                history.append(f"Round {index}: {plan.get('goal', 'edit')} -> no change produced")
                continue

            # RENDER
            try:
                candidate_png, metrics = self.render(candidate_html)
            except RendererError as exc:
                LOG.warning("round %d: candidate failed to render: %s", index, exc)
                record(RoundResult(index, "rejected", mode=mode, plan=plan, error=f"render: {exc}", operator=touched_by_operator(plan)))
                history.append(f"Round {index}: {plan.get('goal', 'edit')} -> rejected (render failed)")
                write_json(rdir / "error.json", {"stage": "render", "error": str(exc)})
                continue
            write_bytes(rdir / "candidate.png", candidate_png)
            write_json(rdir / "metrics.json", metrics)

            # One image a person can actually judge from.
            compare_path, compare_panels = side_by_side(
                [
                    ("1. SOURCE", str(source_png)),
                    ("2. BEFORE", current_png),
                    ("3. AFTER (candidate)", candidate_png),
                ],
                rdir / "compare.png",
            )

            # When the operator marked a region, a full-page comparison is too
            # coarse to show whether that area actually changed.
            region_compare = None
            region = plan.get("operator_region")
            if isinstance(region, dict):
                try:
                    region_compare, _ = side_by_side(
                        [
                            ("1. SOURCE (region)", crop_normalized(source_png, region)),
                            ("2. BEFORE (region)", crop_normalized(current_png, region)),
                            ("3. AFTER (region)", crop_normalized(candidate_png, region)),
                        ],
                        rdir / "compare_region.png",
                    )
                except (OSError, ValueError) as exc:
                    LOG.warning("could not build the region comparison: %s", exc)

            # VERIFY
            try:
                verdict = self.verify(plan, source_png, current_png, candidate_png)
                verdict["verified_by"] = MODEL
                if self.verify_mode == "both":
                    verdict = self.review_verify(
                        verdict,
                        {
                            "round": index,
                            "image": str(compare_path),
                            "image_panels": compare_panels,
                            "image2": str(region_compare) if region_compare else None,
                            "image2_label": "지정한 영역 (수정 전 → 후)",
                        },
                    )
            except (LLMError, ValueError) as exc:
                LOG.error("round %d: VERIFY failed: %s", index, exc)
                record(RoundResult(index, "error", mode=mode, plan=plan, error=f"verify: {exc}", operator=touched_by_operator(plan)))
                write_json(rdir / "error.json", {"stage": "verify", "error": str(exc)})
                continue
            write_json(rdir / "verify.json", verdict)
            last_verify = verdict

            decision = verdict["decision"]
            reason = str(verdict.get("reason", ""))
            record(
                RoundResult(
                    index,
                    decision,
                    reason=reason,
                    mode=mode,
                    operator=touched_by_operator(plan, verdict),
                    plan=plan,
                    verify=verdict,
                    changed_lines=changed_lines,
                )
            )
            # Built from the round that was just recorded, so the verdict's own
            # reasoning travels forward rather than being dropped here.
            history.append(results[-1].history_line())

            if decision in ("keep", "done"):
                current_html, current_png = candidate_html, candidate_png
            else:
                LOG.info("round %d: reverting to previous HTML", index)

            if decision == "done":
                stop_reason = "done"
                LOG.info("VERIFY says done after round %d", index)
                break

        # Final artefacts
        clone_html = write_text(self.out / "clone.html", current_html)
        clone_png = write_bytes(self.out / "clone.png", current_png)
        write_json(self.out / "final_verify.json", last_verify)

        summary = {
            "source": str(source_png),
            "out_dir": str(self.out),
            "clone_html": str(clone_html),
            "clone_png": str(clone_png),
            "stop_reason": stop_reason,
            "rounds_run": len(results),
            "max_rounds": self.cfg.loop.max_rounds,
            "kept": sum(1 for r in results if r.decision in ("keep", "done")),
            "reverted": sum(1 for r in results if r.decision == "revert"),
            "rejected": sum(1 for r in results if r.decision in ("rejected", "noop")),
            "errors": sum(1 for r in results if r.decision == "error"),
            "skipped": sum(1 for r in results if r.decision == "skipped"),
            # How far the loop actually moved the document. A run that ends with
            # kept_line_changes near zero produced a clone that is still the
            # bootstrap draft, whatever the per-round verdicts said.
            "kept_line_changes": sum(
                r.changed_lines for r in results if r.decision in ("keep", "done")
            ),
            "operator_notes": self.notes,
            "operator_interventions": self.interventions,
            "operator_rounds": sum(1 for r in results if r.operator),
            "failed_attempts": failed,
            "verify_mode": self._base_verify_mode,
            "verify_mode_final": self.verify_mode,
            "thinking_control": self.llm.supports_thinking_flag,
            "rounds": [
                {
                    "round": r.index,
                    "decision": r.decision,
                    "mode": r.mode,
                    "operator": r.operator,
                    "scope": r.plan.get("scope", ""),
                    "target": r.plan.get("target", ""),
                    "goal": r.plan.get("goal", ""),
                    "reason": r.reason,
                    "changed_lines": r.changed_lines,
                    "error": r.error,
                }
                for r in results
            ],
        }
        write_json(self.out / "summary.json", summary)
        LOG.info(
            "BUILD done: %s (stop=%s, kept=%d, reverted=%d, rejected=%d, errors=%d, "
            "lines changed=%d)",
            clone_html,
            stop_reason,
            summary["kept"],
            summary["reverted"],
            summary["rejected"],
            summary["errors"],
            summary["kept_line_changes"],
        )
        return summary
