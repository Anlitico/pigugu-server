"""Prompt templates  -  loaded from .j2 files and cached."""

from __future__ import annotations

from pathlib import Path

import jinja2

_DIR = Path(__file__).parent
_env = jinja2.Environment(loader=jinja2.FileSystemLoader(str(_DIR)), autoescape=False)
_cache: dict[str, jinja2.Template] = {}


def load(name: str) -> jinja2.Template:
    """Load a .j2 template by name (without extension), with caching."""
    if name not in _cache:
        _cache[name] = _env.get_template(f"{name}.j2")
    return _cache[name]


def render(name: str, **ctx) -> str:
    """Load and render a .j2 template."""
    return load(name).render(**ctx).strip()
