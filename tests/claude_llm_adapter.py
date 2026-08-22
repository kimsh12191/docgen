"""TEST-ONLY: serve the Qwen OpenAI-compatible contract, backed by Claude.

The pipeline talks to one HTTP contract (POST /v1/chat/completions). This
adapter speaks that contract and forwards to the Claude API via the official
`anthropic` SDK, so the whole loop can be exercised with a real VLM in an
environment that cannot reach the internal Qwen endpoint. Nothing in the
pipeline imports this module -- point DOCGEN_LLM_BASE_URL at it instead.

    export ANTHROPIC_API_KEY=...
    python3 tests/claude_llm_adapter.py 38902
    DOCGEN_LLM_BASE_URL=http://127.0.0.1:38902/v1 \
    DOCGEN_LLM_MODEL=claude-opus-5 \
    python run.py build source.png -o out/source

Contract translation notes:
  * temperature / top_p are removed on Claude Opus 5 (they return 400), so the
    adapter drops them. The pipeline's values are ignored, not forwarded.
  * chat_template_kwargs.enable_thinking has no direct Claude equivalent.
    true  -> adaptive thinking, effort high, summarized display
    false -> adaptive thinking, effort low
    Thinking is never fully disabled: on Opus 5 disabling it can push tool-call
    or <thinking> text into the visible response. Lower effort is the supported
    way to spend less reasoning, so PLAN/VERIFY still think harder than
    BOOTSTRAP/ACTION.
  * Claude returns thinking as separate blocks, so only text blocks become the
    OpenAI-shaped `content` -- there is no <think> wrapper to strip.
  * Streaming is used because max_tokens is large; a non-streaming call at
    32768 risks an HTTP timeout.
"""

from __future__ import annotations

import base64
import json
import os
import re
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import anthropic

CLAUDE_MODEL = os.environ.get("CLAUDE_MODEL", "claude-opus-5")
ADVERTISED_MODEL = os.environ.get("ADAPTER_MODEL_NAME", CLAUDE_MODEL)

_DATA_URI = re.compile(r"^data:(image/[a-zA-Z0-9.+-]+);base64,(.+)$", re.DOTALL)

_client = None


def get_client() -> anthropic.Anthropic:
    """Lazy so the script starts with a clear message when no key is set."""
    global _client
    if _client is None:
        _client = anthropic.Anthropic()
    return _client


def to_anthropic(messages: list[dict]) -> tuple[str | None, list[dict]]:
    """OpenAI-style messages -> (system prompt, Anthropic messages)."""
    system: list[str] = []
    out: list[dict] = []

    for msg in messages:
        role = msg.get("role")
        content = msg.get("content")

        if role == "system":
            system.append(content if isinstance(content, str) else
                          " ".join(p.get("text", "") for p in content or []))
            continue

        if isinstance(content, str):
            out.append({"role": role, "content": [{"type": "text", "text": content}]})
            continue

        blocks = []
        for part in content or []:
            if part.get("type") == "text":
                blocks.append({"type": "text", "text": part["text"]})
            elif part.get("type") == "image_url":
                url = part["image_url"]["url"]
                match = _DATA_URI.match(url)
                if not match:
                    raise ValueError("adapter only accepts base64 data URIs for images")
                media_type, data = match.group(1), match.group(2)
                # Validate now so a malformed payload fails here, not mid-stream.
                base64.b64decode(data, validate=True)
                blocks.append({
                    "type": "image",
                    "source": {"type": "base64", "media_type": media_type, "data": data},
                })
        out.append({"role": role, "content": blocks})

    return ("\n\n".join(s for s in system if s) or None), out


FINISH = {"end_turn": "stop", "stop_sequence": "stop", "max_tokens": "length",
          "tool_use": "tool_calls", "refusal": "content_filter"}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def _send(self, code: int, payload: dict):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.endswith("/models"):
            self._send(200, {"object": "list",
                             "data": [{"id": ADVERTISED_MODEL, "object": "model"}]})
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self):
        if not self.path.endswith("/chat/completions"):
            self._send(404, {"error": "not found"})
            return
        try:
            req = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))))
        except (ValueError, UnicodeDecodeError) as exc:
            self._send(400, {"error": {"message": f"bad json: {exc}"}})
            return

        try:
            system, messages = to_anthropic(req.get("messages", []))
        except (ValueError, KeyError, TypeError, base64.binascii.Error) as exc:
            self._send(400, {"error": {"message": f"cannot translate request: {exc}"}})
            return

        thinking_on = bool(
            (req.get("chat_template_kwargs") or {}).get("enable_thinking", False)
        )
        kwargs = {
            "model": CLAUDE_MODEL,
            "max_tokens": min(int(req.get("max_tokens", 32768)), 64000),
            "messages": messages,
            "thinking": {"type": "adaptive", "display": "summarized" if thinking_on else "omitted"},
            "output_config": {"effort": "high" if thinking_on else "low"},
        }
        if system:
            kwargs["system"] = system

        try:
            with get_client().messages.stream(**kwargs) as stream:
                message = stream.get_final_message()
        except anthropic.APIStatusError as exc:
            self._send(exc.status_code, {"error": {"message": str(exc)[:800]}})
            return
        except anthropic.AnthropicError as exc:
            self._send(502, {"error": {"message": f"{type(exc).__name__}: {str(exc)[:500]}"}})
            return
        except Exception as exc:  # noqa: BLE001
            # Unresolved credentials surface here as a TypeError, not an
            # AnthropicError. Report it over the contract instead of a 500
            # traceback so the pipeline logs a usable message.
            self._send(401, {"error": {"message": f"{type(exc).__name__}: {str(exc)[:500]}"}})
            return

        text = "".join(b.text for b in message.content if b.type == "text")
        self._send(200, {
            "id": message.id,
            "object": "chat.completion",
            "model": ADVERTISED_MODEL,
            "choices": [{
                "index": 0,
                "message": {"role": "assistant", "content": text},
                "finish_reason": FINISH.get(message.stop_reason, "stop"),
            }],
            "usage": {
                "prompt_tokens": message.usage.input_tokens,
                "completion_tokens": message.usage.output_tokens,
            },
        })


def main():
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 38902
    if not (os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")):
        print("warning: no ANTHROPIC_API_KEY / ANTHROPIC_AUTH_TOKEN set; "
              "requests will fail unless the SDK finds another credential source",
              file=sys.stderr)
    print(f"claude adapter on http://127.0.0.1:{port} "
          f"(claude model={CLAUDE_MODEL}, advertised as {ADVERTISED_MODEL})", flush=True)
    ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
