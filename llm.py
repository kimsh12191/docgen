"""OpenAI-compatible client for the Qwen VLM endpoint (stdlib urllib only)."""

from __future__ import annotations

import http.client
import json
import logging
import time
import urllib.error
import urllib.request

from config import LLMConfig
from utils import data_uri, png_bytes, strip_think

LOG = logging.getLogger("docgen.llm")


class LLMError(RuntimeError):
    """Raised when the LLM endpoint cannot be used or returns no usable content."""


# --------------------------------------------------------------- message parts

def text_part(text: str) -> dict:
    return {"type": "text", "text": text}


def image_part(path_or_bytes, max_side: int = 1600) -> dict:
    """Normalise an image to an RGB PNG data URI (OpenAI image_url format)."""
    png = png_bytes(path_or_bytes, max_side=max_side)
    return {"type": "image_url", "image_url": {"url": data_uri(png)}}


def user_message(*parts) -> dict:
    content: list[dict] = []
    for part in parts:
        if part is None:
            continue
        if isinstance(part, str):
            content.append(text_part(part))
        elif isinstance(part, (list, tuple)):
            content.extend(p for p in part if p is not None)
        else:
            content.append(part)
    return {"role": "user", "content": content}


def system_message(text: str) -> dict:
    return {"role": "system", "content": text}


# ------------------------------------------------------------------- response

class LLMResponse:
    def __init__(self, content: str, finish_reason: str, raw: dict, reasoning: str = "") -> None:
        self.content = content
        self.finish_reason = finish_reason
        self.raw = raw
        self.reasoning = reasoning

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<LLMResponse finish={self.finish_reason!r} chars={len(self.content)}>"


# --------------------------------------------------------------------- client

class QwenClient:
    def __init__(self, cfg: LLMConfig) -> None:
        self.cfg = cfg
        self.base_url = cfg.base_url.rstrip("/")
        # Flipped to False the first time the server rejects chat_template_kwargs.
        self.supports_thinking_flag = True

    # ------------------------------------------------------------------ http

    def _headers(self) -> dict:
        return {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.cfg.api_key}",
        }

    def _post(self, path: str, payload: dict) -> dict:
        url = f"{self.base_url}{path}"
        body = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(url, data=body, method="POST", headers=self._headers())
        with urllib.request.urlopen(req, timeout=self.cfg.timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def list_models(self) -> list[str]:
        """GET /models — used by `doctor`."""
        url = f"{self.base_url}/models"
        req = urllib.request.Request(url, method="GET", headers=self._headers())
        try:
            with urllib.request.urlopen(req, timeout=min(self.cfg.timeout, 60)) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:300]
            raise LLMError(f"GET /models HTTP {exc.code}: {detail}") from exc
        except urllib.error.URLError as exc:
            raise LLMError(f"GET /models unreachable at {self.base_url}: {exc.reason}") from exc
        except TimeoutError as exc:
            raise LLMError("GET /models timed out") from exc

        items = data.get("data", []) if isinstance(data, dict) else []
        return [item.get("id", "") for item in items if isinstance(item, dict)]

    # ------------------------------------------------------------------ chat

    def chat(
        self,
        messages: list[dict],
        thinking: bool = False,
        max_tokens: int | None = None,
        temperature: float | None = None,
        stage: str = "chat",
    ) -> LLMResponse:
        """One chat completion. `thinking` maps to chat_template_kwargs.enable_thinking."""
        payload = {
            "model": self.cfg.model,
            "messages": messages,
            "temperature": self.cfg.temperature if temperature is None else temperature,
            "top_p": self.cfg.top_p,
            "max_tokens": max_tokens or self.cfg.max_tokens,
            "stream": False,
        }
        if self.supports_thinking_flag:
            payload["chat_template_kwargs"] = {"enable_thinking": bool(thinking)}

        last_error: Exception | None = None
        attempts = max(1, self.cfg.retries)
        attempt = 0

        while attempt < attempts:
            attempt += 1
            try:
                LOG.info(
                    "[%s] LLM call attempt %d/%d (thinking=%s%s)",
                    stage,
                    attempt,
                    attempts,
                    thinking,
                    "" if self.supports_thinking_flag else ", server default",
                )
                data = self._post("/chat/completions", payload)
                return self._parse(data, stage)

            except urllib.error.HTTPError as exc:
                detail = exc.read().decode("utf-8", "replace")[:500]
                last_error = LLMError(f"HTTP {exc.code}: {detail}")

                # Spec: on a 400 caused by chat_template_kwargs, drop the key and
                # retry once. Stage-level thinking control is lost from here on.
                if exc.code == 400 and "chat_template_kwargs" in payload:
                    LOG.warning(
                        "THINKING CONTROL DISABLED: server returned HTTP 400 with "
                        "chat_template_kwargs (%s). Removing the key and retrying. "
                        "Per-stage thinking control is no longer possible; all stages "
                        "now follow the server default.",
                        detail[:200],
                    )
                    payload.pop("chat_template_kwargs", None)
                    self.supports_thinking_flag = False
                    # This retry is extra: it must not eat into the retry budget
                    # reserved for genuine transient failures.
                    attempts += 1
                    continue

            except (urllib.error.URLError, http.client.HTTPException, OSError) as exc:
                # A dropped connection (http.client.RemoteDisconnected) is not a
                # URLError, so it used to escape and kill the whole build. Any
                # transport-level failure is retryable, not fatal.
                reason = getattr(exc, "reason", exc)
                last_error = LLMError(f"transport error: {type(exc).__name__}: {reason}")
            except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                last_error = LLMError(f"non-JSON response: {exc}")

            if attempt < attempts:
                backoff = 2.0 * (2 ** (attempt - 1))
                LOG.warning("[%s] attempt %d failed (%s); retrying in %.0fs", stage, attempt, last_error, backoff)
                time.sleep(backoff)

        raise LLMError(f"[{stage}] LLM call failed after {attempts} attempts: {last_error}")

    def _parse(self, data: dict, stage: str) -> LLMResponse:
        choices = data.get("choices") or []
        if not choices:
            raise LLMError(f"[{stage}] response contained no choices: {str(data)[:300]}")

        choice = choices[0]
        message = choice.get("message") or {}
        raw_content = message.get("content") or ""
        reasoning = message.get("reasoning_content") or ""
        finish_reason = choice.get("finish_reason") or ""

        if finish_reason == "length":
            LOG.warning(
                "[%s] finish_reason == 'length': output was truncated at max_tokens=%s. "
                "The result is likely incomplete.",
                stage,
                self.cfg.max_tokens,
            )

        content = strip_think(raw_content)
        if not content.strip():
            raise LLMError(
                f"[{stage}] empty content after stripping <think> "
                f"(finish_reason={finish_reason!r}, raw {len(raw_content)} chars)"
            )

        usage = data.get("usage") or {}
        LOG.info(
            "[%s] ok: %d chars, finish=%s, tokens=%s/%s",
            stage,
            len(content),
            finish_reason or "?",
            usage.get("prompt_tokens", "?"),
            usage.get("completion_tokens", "?"),
        )
        return LLMResponse(content=content, finish_reason=finish_reason, raw=data, reasoning=reasoning)
