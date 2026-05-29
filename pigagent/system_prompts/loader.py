"""Template loader — load and render .j2 prompt templates."""

from pathlib import Path

from jinja2 import Environment, BaseLoader, TemplateNotFound

_TEMPLATES_DIR = Path(__file__).parent / "templates"

_env = Environment(loader=BaseLoader())


def _load_template(name: str) -> str:
    path = _TEMPLATES_DIR / name
    if not path.is_file():
        raise TemplateNotFound(str(path))
    return path.read_text(encoding="utf-8")


def render(name: str, **variables) -> str:
    """Render a .j2 template by name (e.g. 'trump.j2')."""
    source = _load_template(name)
    template = _env.from_string(source)
    return template.render(**variables)
