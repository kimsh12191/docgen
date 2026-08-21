"""Configuration loading for docgen."""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path

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


@dataclass
class RendererConfig:
    url: str = "http://10.167.129.230:30900"
    timeout: int = 180
    device_scale: float = 1.0
    width: int = 800
    wait_ms: int = 400


@dataclass
class LoopConfig:
    max_rounds: int = 8


@dataclass
class Config:
    llm: LLMConfig
    renderer: RendererConfig
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
        with open(cfg_path, "rb") as fh:
            raw = tomllib.load(fh)

    cfg = Config(
        llm=_build(LLMConfig, _section(raw, "llm")),
        renderer=_build(RendererConfig, _section(raw, "renderer")),
        loop=_build(LoopConfig, _section(raw, "loop")),
    )

    # Environment overrides make it easy to point at a mock or a relocated service.
    if os.environ.get("DOCGEN_LLM_BASE_URL"):
        cfg.llm.base_url = os.environ["DOCGEN_LLM_BASE_URL"]
    if os.environ.get("DOCGEN_LLM_MODEL"):
        cfg.llm.model = os.environ["DOCGEN_LLM_MODEL"]
    if os.environ.get("DOCGEN_RENDERER_URL"):
        cfg.renderer.url = os.environ["DOCGEN_RENDERER_URL"]

    cfg.llm.base_url = cfg.llm.base_url.rstrip("/")
    cfg.renderer.url = cfg.renderer.url.rstrip("/")
    return cfg
