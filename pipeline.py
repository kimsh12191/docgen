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
    clean_html_output,
    ensure_dir,
    extract_json,
    html_sanity_check,
    image_size,
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

    def history_line(self) -> str:
        goal = (self.plan.get("goal") or self.plan.get("target") or "edit").strip()
        goal = goal.replace("\n", " ")
        if len(goal) > 90:
            goal = goal[:90] + "..."
        return f"Round {self.index}: {goal} -> {self.decision}"


class Pipeline:
    def __init__(
        self,
        cfg: Config,
        out_dir: str | Path,
        notes: str = "",
        interactive: bool = False,
    ) -> None:
        self.cfg = cfg
        self.notes = (notes or "").strip()
        self.interactive = interactive
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

    # ---------------------------------------------------------------- render

    def render(self, html: str) -> tuple[bytes, dict]:
        return self.renderer.probe(
            html,
            width=self.cfg.renderer.width,
            wait_ms=self.cfg.renderer.wait_ms,
        )

    def _img(self, path_or_bytes):
        return image_part(path_or_bytes, max_side=self.max_side)

    # ------------------------------------------------------------- bootstrap

    def bootstrap(self, source_png: Path) -> tuple[str, bytes]:
        """Ask the VLM for a first draft, then render it. Thinking OFF."""
        LOG.info("BOOTSTRAP: generating initial HTML")
        messages = [
            system_message(prompts.BOOTSTRAP_SYSTEM),
            user_message(
                prompts.BOOTSTRAP_USER.format(width=self.cfg.renderer.width),
                self._img(source_png),
            ),
        ]
        resp = self.llm.chat(messages, thinking=False, stage="bootstrap")
        write_text(self.rounds_dir / "bootstrap_raw.txt", resp.content)

        html = clean_html_output(resp.content)
        ok, reason = html_sanity_check(html)
        if not ok:
            raise RuntimeError(f"BOOTSTRAP produced unusable HTML: {reason}")

        write_text(self.rounds_dir / "bootstrap.html", html)
        png, metrics = self.render(html)
        write_bytes(self.rounds_dir / "bootstrap.png", png)
        write_json(self.rounds_dir / "bootstrap_metrics.json", metrics)
        LOG.info(
            "BOOTSTRAP: %d chars html, render %sx%s",
            len(html),
            *image_size(png),
        )
        return html, png

    # ------------------------------------------------------------------ plan

    def plan(self, source_png: Path, current_png: bytes, history: list[str]) -> dict:
        """PLAN. Thinking ON. Images are the primary evidence."""
        text = prompts.PLAN_USER
        notes = prompts.notes_block(self.notes)
        if notes:
            text = f"{text}\n\n{notes}"
        hist = prompts.history_block(history)
        if hist:
            text = f"{text}\n\n{hist}"

        messages = [
            system_message(prompts.PLAN_SYSTEM),
            user_message(
                text,
                self._img(source_png),
                self._img(current_png),
            ),
        ]
        resp = self.llm.chat(messages, thinking=True, stage="plan")
        plan = extract_json(resp.content)
        LOG.info(
            "PLAN: scope=%s target=%s",
            plan.get("scope", "?"),
            truncate(str(plan.get("target", "?")), 80),
        )
        return plan

    # ---------------------------------------------------------------- action

    @staticmethod
    def action_mode(plan: dict) -> str:
        """PLAN already decides global vs local; that is the mode switch.

        local  -> patch:   exact search/replace, response size tracks the edit
        global -> rewrite: full document, needed when the layout is restructured
        """
        scope = str(plan.get("scope", "")).strip().lower()
        return "patch" if scope.startswith("local") else "rewrite"

    def action(self, plan: dict, current_html: str, source_png: Path, current_png: bytes):
        """ACTION. Thinking OFF. Returns (mode, LLMResponse)."""
        mode = self.action_mode(plan)
        if mode == "rewrite" and len(current_html) > HTML_WARN_SIZE:
            LOG.warning(
                "ACTION is rewriting a %d-char document; a full rewrite this large "
                "risks hitting max_tokens=%s and being rejected",
                len(current_html),
                self.cfg.llm.max_tokens,
            )
        template = prompts.ACTION_PATCH_USER if mode == "patch" else prompts.ACTION_REWRITE_USER
        text = template.format(
            plan=json.dumps(plan, ensure_ascii=False, indent=2),
            html=current_html,
        )
        messages = [
            system_message(prompts.ACTION_SYSTEM),
            user_message(
                text,
                self._img(source_png),
                self._img(current_png),
            ),
        ]
        LOG.info("ACTION: mode=%s (scope=%s)", mode, plan.get("scope", "?"))
        return mode, self.llm.chat(messages, thinking=False, stage=f"action:{mode}")

    # ----------------------------------------------------------------- apply

    @staticmethod
    def apply(action_raw: str, previous_html: str, mode: str = "rewrite") -> str:
        """APPLY (Python). patch -> exact edits, rewrite -> fence removal. Raises on reject."""
        if mode == "patch":
            payload = extract_json(action_raw)
            html, applied = apply_edits(previous_html, payload.get("edits"))
            LOG.info("APPLY: %d patch edit(s): %s", len(applied), "; ".join(applied)[:300])
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
        notes = prompts.notes_block(self.notes)
        if notes:
            text = f"{text}\n\n{notes}"
        messages = [
            system_message(prompts.VERIFY_SYSTEM),
            user_message(
                text,
                self._img(source_png),
                self._img(before_png),
                self._img(candidate_png),
            ),
        ]
        resp = self.llm.chat(messages, thinking=True, stage="verify")
        verdict = extract_json(resp.content)

        decision = str(verdict.get("decision", "")).strip().lower()
        if decision not in ("keep", "revert", "done"):
            LOG.warning("VERIFY returned unknown decision %r; treating as revert", decision)
            decision = "revert"
            verdict["decision"] = decision
            verdict.setdefault("reason", "unparsable decision")
        LOG.info("VERIFY: %s (%s)", decision, truncate(str(verdict.get("reason", "")), 100))
        return verdict

    # -------------------------------------------------------- operator input

    def _ask(self, prompt: str) -> str:
        if not self.interactive:
            return ""
        try:
            return input(prompt).strip()
        except (EOFError, KeyboardInterrupt):
            return ""

    def review_plan(self, plan: dict) -> dict:
        """Let the operator steer the plan before ACTION turns it into an edit."""
        if not self.interactive:
            return plan
        print("\n--- PLAN ---")
        print(json.dumps(plan, ensure_ascii=False, indent=2))
        answer = self._ask("추가 지시 (Enter=수락, s=이 라운드 건너뛰기): ")
        if answer.lower() == "s":
            raise SkipRound("operator skipped the round")
        if answer:
            # Lands in the plan JSON, which ACTION receives verbatim.
            plan["operator_instruction"] = answer
            self.interventions += 1
            LOG.info("PLAN: operator added an instruction")
        return plan

    def review_verify(self, verdict: dict) -> dict:
        """Let the operator overrule the model's own judgement of its edit."""
        if not self.interactive:
            return verdict
        print("\n--- VERIFY ---")
        print(json.dumps(verdict, ensure_ascii=False, indent=2))
        answer = self._ask("판정 (Enter=수락, keep/revert/done=강제): ").lower()
        if answer in ("keep", "revert", "done") and answer != verdict.get("decision"):
            # Keep the model's own verdict so evaluation is not contaminated.
            verdict["model_decision"] = verdict.get("decision")
            verdict["operator_override"] = answer
            verdict["decision"] = answer
            self.interventions += 1
            LOG.info("VERIFY: operator overrode %s -> %s", verdict["model_decision"], answer)
        return verdict

    # ------------------------------------------------------------------ loop

    def build(self, source: str | Path) -> dict:
        source_png = Path(source)
        if not source_png.exists():
            raise FileNotFoundError(f"source image not found: {source_png}")

        current_html, current_png = self.bootstrap(source_png)
        history: list[str] = []
        results: list[RoundResult] = []
        last_verify: dict = {}
        stop_reason = "max_rounds"

        for index in range(1, self.cfg.loop.max_rounds + 1):
            rdir = ensure_dir(self.rounds_dir / f"r{index:02d}")
            LOG.info("=== round %d/%d ===", index, self.cfg.loop.max_rounds)

            write_text(rdir / "before.html", current_html)
            write_bytes(rdir / "before.png", current_png)

            # PLAN
            try:
                plan = self.plan(source_png, current_png, history)
                plan = self.review_plan(plan)
            except SkipRound as exc:
                LOG.info("round %d: %s", index, exc)
                results.append(RoundResult(index, "skipped", operator=True))
                history.append(f"Round {index}: skipped by the operator")
                continue
            except (LLMError, ValueError) as exc:
                LOG.error("round %d: PLAN failed: %s", index, exc)
                results.append(RoundResult(index, "error", error=f"plan: {exc}"))
                write_json(rdir / "error.json", {"stage": "plan", "error": str(exc)})
                continue
            write_json(rdir / "plan.json", plan)

            # ACTION
            try:
                mode, action_resp = self.action(plan, current_html, source_png, current_png)
                action_raw = action_resp.content
            except LLMError as exc:
                LOG.error("round %d: ACTION failed: %s", index, exc)
                results.append(RoundResult(index, "error", plan=plan, error=f"action: {exc}"))
                write_json(rdir / "error.json", {"stage": "action", "error": str(exc)})
                continue
            write_text(rdir / "action_raw.txt", action_raw)
            if mode == "patch":
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
                    )
                candidate_html = self.apply(action_raw, current_html, mode=mode)
            except ValueError as exc:
                LOG.warning("round %d: APPLY rejected the candidate: %s", index, exc)
                results.append(RoundResult(index, "rejected", mode=mode, plan=plan, error=str(exc)))
                history.append(f"Round {index}: {plan.get('goal', 'edit')} -> rejected (invalid HTML)")
                write_json(rdir / "error.json", {"stage": "apply", "error": str(exc)})
                continue
            write_text(rdir / "candidate.html", candidate_html)

            if candidate_html.strip() == current_html.strip():
                LOG.warning("round %d: candidate is identical to current HTML; skipping", index)
                results.append(RoundResult(index, "noop", mode=mode, plan=plan))
                history.append(f"Round {index}: {plan.get('goal', 'edit')} -> no change produced")
                continue

            # RENDER
            try:
                candidate_png, metrics = self.render(candidate_html)
            except RendererError as exc:
                LOG.warning("round %d: candidate failed to render: %s", index, exc)
                results.append(RoundResult(index, "rejected", mode=mode, plan=plan, error=f"render: {exc}"))
                history.append(f"Round {index}: {plan.get('goal', 'edit')} -> rejected (render failed)")
                write_json(rdir / "error.json", {"stage": "render", "error": str(exc)})
                continue
            write_bytes(rdir / "candidate.png", candidate_png)
            write_json(rdir / "metrics.json", metrics)

            # VERIFY
            try:
                verdict = self.review_verify(
                    self.verify(plan, source_png, current_png, candidate_png)
                )
            except (LLMError, ValueError) as exc:
                LOG.error("round %d: VERIFY failed: %s", index, exc)
                results.append(RoundResult(index, "error", mode=mode, plan=plan, error=f"verify: {exc}"))
                write_json(rdir / "error.json", {"stage": "verify", "error": str(exc)})
                continue
            write_json(rdir / "verify.json", verdict)
            last_verify = verdict

            decision = verdict["decision"]
            reason = str(verdict.get("reason", ""))
            results.append(
                RoundResult(
                    index,
                    decision,
                    reason=reason,
                    mode=mode,
                    operator=bool(verdict.get("operator_override") or plan.get("operator_instruction")),
                    plan=plan,
                    verify=verdict,
                )
            )
            history.append(RoundResult(index, decision, plan=plan).history_line())

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
            "operator_notes": self.notes,
            "operator_interventions": self.interventions,
            "operator_rounds": sum(1 for r in results if r.operator),
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
                    "error": r.error,
                }
                for r in results
            ],
        }
        write_json(self.out / "summary.json", summary)
        LOG.info(
            "BUILD done: %s (stop=%s, kept=%d, reverted=%d, rejected=%d, errors=%d)",
            clone_html,
            stop_reason,
            summary["kept"],
            summary["reverted"],
            summary["rejected"],
            summary["errors"],
        )
        return summary
