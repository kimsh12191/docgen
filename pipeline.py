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
    crop_normalized,
    image_size,
    side_by_side,
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

        bits: list[str] = []
        if self.decision == "revert" and self.reason:
            bits.append(f"why: {short(self.reason)}")
        remaining = str(self.verify.get("next_major_issue", "")).strip()
        if remaining:
            bits.append(f"next: {short(remaining)}")
        note = str(self.verify.get("operator_note", "")).strip()
        if note and note != self.reason.strip():
            bits.append(f"operator: {short(note)}")

        if bits:
            judged = self.verify.get("verified_by") or "model"
            line += f" [{judged}] " + "; ".join(bits)
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
        if verify_mode not in ("model", "human", "both"):
            raise ValueError(f"verify_mode must be model/human/both, got {verify_mode!r}")
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
        contract = prompts.operator_contract_block(self.operator_active)
        if contract:
            text = f"{text}\n\n{contract}"

        parts = [self._img(source_png), self._img(current_png)]
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

    def _input(self, prompt: str, context: dict | None = None) -> str:
        """Always reads. Used where a human verdict is the only source of truth."""
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
        "개입 (Enter=Qwen 계획대로 / a <의견>=참고로 첨부 / o <지시>=지시 우선 / "
        "x <지시>=Qwen 계획 버리고 내 지시만 / s=건너뛰기): "
    )

    def review_plan(self, plan: dict, context: dict | None = None) -> dict:
        """Let the operator amend or replace the plan before ACTION acts on it."""
        plan.setdefault("planned_by", "model")
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
                {"label": "Qwen 계획대로", "value": "", "style": "primary"},
                {"label": "참고로 첨부 (계획 유지)", "value": "a @text"},
                {"label": "내 지시 우선 (계획 유지)", "value": "o @text"},
                {"label": "Qwen 계획 버리고 내 지시만", "value": "x @text"},
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
            if head in ("a", "o", "x"):
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
                            "planned_by": "operator",
                        }
                    )
                    LOG.info("PLAN: operator discarded the model plan")
                elif head == "o":
                    # ACTION is told this replaces the plan's own goal.
                    plan["operator_instruction"] = rest
                    plan["planned_by"] = "model+operator"
                    LOG.info("PLAN: operator replaced the goal")
                else:
                    plan["operator_note"] = rest
                    plan["planned_by"] = "model+operator"
                    LOG.info("PLAN: operator attached a note")
                if self._last_region:
                    plan["operator_region"] = self._last_region
                    LOG.info("PLAN: operator marked a region %s", self._last_region)
                self.interventions += 1
                return plan
            print("a(참고 첨부) / o(지시 우선) / x(계획 버리고 내 지시만) / s(건너뛰기) 중에서 골라주세요.")
            if attempt == 2:
                print("입력을 이해하지 못했습니다. 계획을 그대로 수락합니다.")
        return plan

    VERIFY_PROMPT = (
        "개입 (Enter=Qwen 판정대로 / a <의견>=참고로 첨부 / "
        "keep|revert|done=Qwen 판정 버리고 내 판정: ): "
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
                {"label": "Qwen 판정대로", "value": "", "style": "primary"},
                {"label": "참고로 첨부 (판정 유지)", "value": "a @text"},
                {"label": "내 판정: keep", "value": "keep"},
                {"label": "내 판정: revert", "value": "revert", "style": "warn"},
                {"label": "내 판정: done", "value": "done"},
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
                verdict["verified_by"] = "model+operator"
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
                verdict["verified_by"] = "operator"
                if rest:
                    verdict["operator_note"] = rest
                self.interventions += 1
                LOG.info("VERIFY: operator overrode %s -> %s", verdict["model_decision"], head)
                return verdict
            print("a(참고 첨부) / keep / revert / done(내 판정) 중에서 골라주세요.")
            if attempt == 2:
                print("입력을 이해하지 못했습니다. 모델 판정을 그대로 둡니다.")
        return verdict

    def human_verify(
        self,
        plan: dict,
        compare_path: Path,
        round_index=None,
        panels=None,
        region_compare=None,
    ) -> dict:
        """The operator is the verifier: no VERIFY call is made to the model."""
        print("\n--- VERIFY (사람 판정) ---")
        print(f"비교 이미지: {compare_path}")
        goal = plan.get("goal", "") or plan.get("target", "")
        print(f"이번 라운드 목표: {goal}")

        base = {
            "stage": "VERIFY",
            "round": round_index if round_index is not None else "",
            "image": str(compare_path),
            "image_panels": panels or [],
            "image2": str(region_compare) if region_compare else None,
            "image2_label": "지정한 영역 (수정 전 → 후)",
            "data": {"goal": goal, "plan": plan},
        }
        decision = ""
        for _ in range(3):
            answer = self._input(
                "판정 (keep=반영 / revert=되돌림 / done=완료): ",
                dict(
                    base,
                    title=f"VERIFY (사람 판정) — 라운드 {base['round']}",
                    text=False,
                    choices=[
                        {"label": "keep (반영)", "value": "keep", "style": "primary"},
                        {"label": "revert (되돌림)", "value": "revert", "style": "warn"},
                        {"label": "done (완료)", "value": "done"},
                    ],
                ),
            ).lower()
            if answer in ("keep", "revert", "done"):
                decision = answer
                break
            print("keep / revert / done 중 하나를 입력하세요.")
        if not decision:
            # Never adopt an unjudged edit.
            LOG.warning("VERIFY: no usable operator verdict; defaulting to revert")
            decision = "revert"

        reason = self._input(
            "이유 (선택, Enter=생략): ",
            dict(base, title="이유 (선택)", text=True, enter_value="@text",
                 choices=[{"label": "생략", "value": ""},
                          {"label": "입력한 이유 전송", "value": "@text", "style": "primary"}]),
        )
        next_issue = self._input(
            "다음에 고칠 것 (선택, Enter=생략): ",
            dict(base, title="다음에 고칠 것 (선택)", text=True, enter_value="@text",
                 choices=[{"label": "생략", "value": ""},
                          {"label": "입력한 내용 전송", "value": "@text", "style": "primary"}]),
        )
        self.interventions += 1
        LOG.info("VERIFY: operator decided %s", decision)
        return {
            "decision": decision,
            "reason": reason or "operator verdict",
            "next_major_issue": next_issue,
            "verified_by": "operator",
        }

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
                record(RoundResult(index, "error", plan=plan, error=f"action: {exc}", operator=plan.get("planned_by", "model") != "model"))
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
                record(RoundResult(index, "rejected", mode=mode, plan=plan, error=str(exc), operator=plan.get("planned_by", "model") != "model"))
                history.append(f"Round {index}: {plan.get('goal', 'edit')} -> rejected (invalid HTML)")
                write_json(rdir / "error.json", {"stage": "apply", "error": str(exc)})
                continue
            write_text(rdir / "candidate.html", candidate_html)

            if candidate_html.strip() == current_html.strip():
                LOG.warning("round %d: candidate is identical to current HTML; skipping", index)
                record(RoundResult(index, "noop", mode=mode, plan=plan, operator=plan.get("planned_by", "model") != "model"))
                history.append(f"Round {index}: {plan.get('goal', 'edit')} -> no change produced")
                continue

            # RENDER
            try:
                candidate_png, metrics = self.render(candidate_html)
            except RendererError as exc:
                LOG.warning("round %d: candidate failed to render: %s", index, exc)
                record(RoundResult(index, "rejected", mode=mode, plan=plan, error=f"render: {exc}", operator=plan.get("planned_by", "model") != "model"))
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
                if self.verify_mode == "human":
                    verdict = self.human_verify(
                        plan,
                        compare_path,
                        round_index=index,
                        panels=compare_panels,
                        region_compare=region_compare,
                    )
                else:
                    verdict = self.verify(plan, source_png, current_png, candidate_png)
                    verdict["verified_by"] = "model"
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
                record(RoundResult(index, "error", mode=mode, plan=plan, error=f"verify: {exc}", operator=plan.get("planned_by", "model") != "model"))
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
                    operator=bool(
                        verdict.get("operator_override")
                        or verdict.get("operator_note")
                        or verdict.get("verified_by") in ("operator", "model+operator")
                        or plan.get("planned_by", "model") != "model"
                    ),
                    plan=plan,
                    verify=verdict,
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
