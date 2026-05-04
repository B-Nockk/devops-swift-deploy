from __future__ import annotations

import sys
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, StrictUndefined, TemplateNotFound

from .config import ResolvedConfig

#! Should be in env file
NGINX_TEMPLATE = "nginx.conf.j2"
COMPOSE_TEMPLATE = "docker-compose.yml.j2"

NGINX_OUTPUT = "nginx.conf"
COMPOSE_OUTPUT = "docker-compose.yml"


# ===========================================================================
# helpers
# ===========================================================================
def _make_env(templates_dir: Path) -> Environment:
    """
    Build a Jinja2 environment.

    StrictUndefined causes Jinja2 to:
       - raise on any variable reference that isn't provided
       - prevents silently writing empty fields in generated configs.
    """
    if not templates_dir.is_dir():
        print(
            f"[ERROR] Templates directory not found: {templates_dir}", file=sys.stderr
        )
        sys.exit(1)

    return Environment(
        loader=FileSystemLoader(str(templates_dir)),
        undefined=StrictUndefined,
        keep_trailing_newline=True,
        trim_blocks=True,
        lstrip_blocks=True,
    )


def _render(
    env: Environment,
    template_name: str,
    variables: dict,
    output_path: Path,
) -> Path:
    """
    Render a single template and write to output_path.

    Returns the output path on success.
    Exits non-zero with a clear message on any failure.
    """
    try:
        template = env.get_template(template_name)
    except TemplateNotFound:
        print(
            f"[ERROR] Template not found: {template_name}\n"
            f"  Expected in: {env.loader.searchpath}",  # type: ignore[union-attr]
            file=sys.stderr,
        )
        sys.exit(1)

    try:
        rendered = template.render(**variables)
    except Exception as exc:
        print(
            f"[ERROR] Template rendering failed for {template_name}:\n  {exc}",
            file=sys.stderr,
        )
        sys.exit(1)

    try:
        output_path.write_text(rendered, encoding="utf-8")
    except OSError as exc:
        print(f"[ERROR] Could not write {output_path}:\n  {exc}", file=sys.stderr)
        sys.exit(1)

    return output_path


# ===========================================================================
# generators
# ===========================================================================


def generate_compose_only(
    config: ResolvedConfig,
    templates_dir: Path,
    output_dir: Path,
) -> Path:
    """
    Re-render only docker-compose.yml.
    Used by `promote` — nginx.conf does not change when mode changes.
    """
    env = _make_env(templates_dir)
    return _render(
        env,
        COMPOSE_TEMPLATE,
        config.as_template_vars(),
        output_dir / COMPOSE_OUTPUT,
    )


def generate_all(
    config: ResolvedConfig,
    templates_dir: Path,
    output_dir: Path,
) -> tuple[Path, Path]:
    """
    Render both templates and write output files.

    Args:
        config:        Fully resolved configuration.
        templates_dir: Directory containing *.j2 template files.
        output_dir:    Directory where generated files are written.
                       Must exist (caller's responsibility).

    Returns:
        Tuple of (nginx_conf_path, compose_path) for the written files.

    Raises:
        SystemExit on any template or write error.
    """
    env = _make_env(templates_dir)
    vars = config.as_template_vars()

    nginx_path = _render(env, NGINX_TEMPLATE, vars, output_dir / NGINX_OUTPUT)
    compose_path = _render(env, COMPOSE_TEMPLATE, vars, output_dir / COMPOSE_OUTPUT)

    return nginx_path, compose_path
