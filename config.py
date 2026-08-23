"""Configuration loading for docgen."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

# tomllib is only in the standard library from Python 3.11. Rather than make the
# whole project need 3.11 for one config file, fall back: tomli if it happens to
# be installed, then a parser for the small subset this project's config.toml
# actually uses.
try:  # Python 3.11+
    import tomllib as _toml
except ModuleNotFoundError:  # pragma: no cover - depends on the interpreter
    try:
        import tomli as _toml  # type: ignore[no-redef]
    except ModuleNotFoundError:
        _toml = None  # type: ignore[assignment]


_SECTION = re.compile(r"^\[([A-Za-z0-9_.-]+)\]$")
_ENTRY = re.compile(r'^([A-Za-z0-9_-]+)\s*=\s*(.+?)\s*$')


def _parse_value(raw: str, where: str):
    if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in "\"'":
        return raw[1:-1]
    if raw in ("true", "false"):
        return raw == "true"
    try:
        return int(raw)
    except ValueError:
        pass
    try:
        return float(raw)
    except ValueError:
        pass
    raise ValueError(f"{where}: cannot read value {raw!r}")


def _minimal_toml(text: str, path) -> dict:
    """Read the flat [section] key = value shape this project's config uses.

    Deliberately narrow: anything outside that shape raises instead of being
    guessed at, so a real TOML file is never silently half-read. Install `tomli`
    or use Python 3.11+ if the config needs more than this.
    """
    out: dict = {}
    section = None
    for number, line in enumerate(text.splitlines(), 1):
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        where = f"{path}:{number}"
        match = _SECTION.match(line)
        if match:
            section = out.setdefault(match.group(1), {})
            continue
        match = _ENTRY.match(line)
        if not match:
            raise ValueError(
                f"{where}: this build reads only '[section]' and 'key = value' lines "
                f"({line!r}). Install tomli, or run on Python 3.11+."
            )
        if section is None:
            raise ValueError(f"{where}: '{match.group(1)}' is outside any [section]")
        section[match.group(1)] = _parse_value(match.group(2), where)
    return out

#: Accepted values for llm.thinking.
THINKING_MODES = ("all", "judging")

PROJECT_ROOT = Path(__file__).resolve().parent
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config.toml"


@dataclass
class LLMConfig:
    base_url: str = "http://10.167.129.250:30164/v1"
    api_key: str = "EMPTY"
    model: str = "qwen3.5_397b_a17b"
    temperature: float = 0.2
    top_p: float = 0.8
    max_tokens: int = 32768
    timeout: int = 900
    retries: int = 3
    image_max_side: int = 1600
    #: "all" thinks at every stage; "judging" only where a decision is made
    #: (PLAN, the layout check, VERIFY). See Pipeline.thinking_for.
    thinking: str = "all"


@dataclass
class RendererConfig:
    url: str = "http://10.167.129.230:30900"
    timeout: int = 180
    device_scale: float = 1.0
    width: int = 800
    wait_ms: int = 400


@dataclass
class BootstrapConfig:
    #: False falls back to asking for the whole document in one call.
    staged: bool = True
    #: How small the source is shrunk for the structure-only steps. Detail has
    #: to be gone for "does the overall layout match" to be answerable.
    rough_max_side: int = 700
    #: Upper bound on the fill phase, so a skeleton that marks fifty blocks
    #: cannot turn into fifty LLM calls.
    max_blocks: int = 12


@dataclass
class LoopConfig:
    max_rounds: int = 8


@dataclass
class Config:
    llm: LLMConfig
    renderer: RendererConfig
    bootstrap: BootstrapConfig
    loop: LoopConfig


def _section(raw: dict, name: str) -> dict:
    value = raw.get(name, {})
    if not isinstance(value, dict):
        raise ValueError(f"config section [{name}] must be a table")
    return value


def _build(cls, data: dict):
    known = {f for f in cls.__dataclass_fields__}
    unknown = set(data) - known
    if unknown:
        raise ValueError(f"unknown keys for {cls.__name__}: {sorted(unknown)}")
    return cls(**data)


def load_config(path: str | os.PathLike | None = None) -> Config:
    """Load config.toml, falling back to the built-in defaults."""
    cfg_path = Path(path) if path else DEFAULT_CONFIG_PATH
    raw: dict = {}
    if cfg_path.exists():
        if _toml is not None:
            with open(cfg_path, "rb") as fh:
                raw = _toml.load(fh)
        else:
            raw = _minimal_toml(cfg_path.read_text(encoding="utf-8"), cfg_path)

    cfg = Config(
        llm=_build(LLMConfig, _section(raw, "llm")),
        renderer=_build(RendererConfig, _section(raw, "renderer")),
        bootstrap=_build(BootstrapConfig, _section(raw, "bootstrap")),
        loop=_build(LoopConfig, _section(raw, "loop")),
    )

    # Environment overrides make it easy to point at a mock or a relocated service.
    if os.environ.get("DOCGEN_LLM_BASE_URL"):
        cfg.llm.base_url = os.environ["DOCGEN_LLM_BASE_URL"]
    if os.environ.get("DOCGEN_LLM_MODEL"):
        cfg.llm.model = os.environ["DOCGEN_LLM_MODEL"]
    if os.environ.get("DOCGEN_RENDERER_URL"):
        cfg.renderer.url = os.environ["DOCGEN_RENDERER_URL"]
    if os.environ.get("DOCGEN_LLM_THINKING"):
        cfg.llm.thinking = os.environ["DOCGEN_LLM_THINKING"]

    if cfg.llm.thinking not in THINKING_MODES:
        raise ValueError(
            f"llm.thinking must be one of {sorted(THINKING_MODES)}, "
            f"got {cfg.llm.thinking!r}"
        )

    cfg.llm.base_url = cfg.llm.base_url.rstrip("/")
    cfg.renderer.url = cfg.renderer.url.rstrip("/")
    return cfg
