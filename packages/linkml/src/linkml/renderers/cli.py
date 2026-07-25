"""CLI entrypoint for ``linkml-render``."""

from pathlib import Path
from typing import Any

import click
import yaml

from linkml._version import __version__
from linkml.renderers.template_renderer import render_file


def _parse_template_vars(values: tuple[str, ...]) -> dict[str, Any]:
    parsed: dict[str, Any] = {}
    for entry in values:
        if "=" not in entry:
            raise click.BadParameter(f"invalid --var '{entry}': expected KEY=VALUE")
        key, raw_value = entry.split("=", 1)
        key = key.strip()
        if not key:
            raise click.BadParameter(f"invalid --var '{entry}': KEY must be non-empty")
        parsed[key] = yaml.safe_load(raw_value)
    return parsed


# Default config filename looked up in the current directory when ``--config``
# is not given (mirrors the ``gen-project`` convention of a top-level config.yaml).
_DEFAULT_CONFIG_FILE = "config.yaml"


def _resolve_config_file(config_file: str | None) -> str | None:
    """Resolve the config path, falling back to a top-level ``config.yaml``.

    An explicit ``--config`` value is returned as-is (its existence is validated
    by Click). When omitted, ``config.yaml`` in the current directory is used if
    present; otherwise ``None`` (no config).
    """
    if config_file is not None:
        return config_file
    default = Path(_DEFAULT_CONFIG_FILE)
    return str(default) if default.is_file() else None


def _load_config(config_file: str | None) -> dict[str, Any]:
    """Load a YAML config file (e.g. a ``gen-project`` ``config.yaml``)."""
    if config_file is None:
        return {}
    with open(config_file, encoding="utf-8") as stream:
        data = yaml.safe_load(stream)
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise click.BadParameter(f"config file '{config_file}' must contain a YAML mapping")
    return data


def _render_section(config: dict[str, Any], target: str | None) -> dict[str, Any]:
    """Return the ``render.<target>`` settings mapping.

    The ``render:`` block is keyed by target name (parallel to ``generator_args``);
    ``--target NAME`` selects ``render[NAME]``. Without a target, or when the block
    or entry is absent, an empty mapping is returned.
    """
    render = config.get("render")
    if not isinstance(render, dict) or target is None:
        return {}
    sub = render.get(target)
    if sub is None:
        return {}
    if not isinstance(sub, dict):
        raise click.BadParameter(f"config 'render.{target}' must be a mapping")
    return sub


def _config_vars(config: dict[str, Any], target: str | None, section: dict[str, Any]) -> dict[str, Any]:
    """Build template ``vars``: the target's ``generator_args`` then the block's ``vars``.

    Merging ``generator_args`` for the selected target lets existing ``gen-project``
    values (e.g. the java ``package``) be reused as ``vars`` without duplication.
    """
    result: dict[str, Any] = {}
    if target is not None:
        gen_args = config.get("generator_args")
        if isinstance(gen_args, dict) and isinstance(gen_args.get(target), dict):
            result.update(gen_args[target])
    raw = section.get("vars")
    if raw is not None:
        if not isinstance(raw, dict):
            raise click.BadParameter("config 'vars' must be a mapping")
        result.update(raw)
    return result


@click.command(name="render")
@click.option("-s", "--schema", default=None, help="Path to the LinkML schema (YAML)")
@click.option("--data", "input_data", default=None, help="Path to the instance-data file")
@click.option(
    "--template",
    "template_file",
    default=None,
    help="Jinja2 template rendered once per item (context: item, root, sv, vars, config)",
)
@click.option(
    "--collection",
    default=None,
    help="Root slot to iterate. If omitted, the root object is rendered once.",
)
@click.option(
    "--filename",
    "filename_expr",
    default=None,
    help=(
        "Jinja2 expression for each item's output path, e.g. "
        "\"{{ vars.package.replace('.', '/') }}/{{ item.name }}.java\""
    ),
)
@click.option(
    "--config",
    "config_file",
    default=None,
    type=click.Path(exists=True, dir_okay=False),
    help=(
        "YAML config file (e.g. a gen-project config.yaml). Optional: defaults to "
        "./config.yaml if present. Exposed to templates as `config`; a `render:` "
        "block may supply render options and `vars`."
    ),
)
@click.option(
    "-t",
    "--target",
    default=None,
    help=(
        "Target name (e.g. java, proto, graphql). Selects the `render.<target>` "
        "block and merges that target's `generator_args` into `vars`."
    ),
)
@click.option(
    "--var",
    "template_vars_raw",
    multiple=True,
    help="Template variable (KEY=VALUE). Repeat to pass multiple values; available as vars.KEY.",
)
@click.option("-d", "--output-dir", default=None, help="Output directory (default: current directory)")
@click.option("-C", "--target-class", default=None, help="Root class name (else inferred)")
@click.option("-f", "--input-format", default=None, help="Data format (else inferred from suffix)")
@click.option(
    "--validate/--no-validate",
    default=None,
    help="Validate the instance data against the schema before rendering (default: enabled)",
)
@click.option(
    "--typed/--no-typed",
    default=None,
    help="Load data as typed objects (applies defaults); --no-typed loads plain dicts (default: typed)",
)
@click.version_option(__version__, "-V", "--version")
@click.pass_context
def cli(
    ctx,
    schema,
    input_data,
    template_file,
    collection,
    filename_expr,
    config_file,
    target,
    template_vars_raw,
    output_dir,
    target_class,
    input_format,
    validate,
    typed,
):
    """Render LinkML instance data through a Jinja2 template, one file per item.

    Unlike the schema-driven ``gen-*`` generators, this consumes *instance data*:
    it loads the data (optionally validating it), iterates a collection, and
    renders each item to its own output file.

    Values may come from a ``--config`` file (including a ``gen-project``
    ``config.yaml``); explicit command-line options always take precedence. The
    parsed config is available to templates as ``config``, so a value such as
    ``generator_args.java.package`` can drive output without living in the data.

    Different targets (java, proto, graphql, ...) usually need different
    templates and variables. Key the ``render:`` block by target name and select
    one with ``--target``; that target's ``generator_args`` entry is merged into
    ``vars`` automatically.

    Example (single target on the command line)::

        linkml-render -s schema.yaml --data catalog.yaml \\
            --template annotation.java.jinja2 --collection annotation_types \\
            --config config.yaml \\
            --filename "{{ config.generator_args.java.package.replace('.', '/') }}/{{ item.name }}.java" \\
            -d out/

    Example (per-target config block)::

        linkml-render -s schema.yaml --data catalog.yaml \\
            --config config.yaml --target java
    """
    config_file = _resolve_config_file(config_file)
    config = _load_config(config_file)
    section = _render_section(config, target)

    # Config render-block values fill in only where the CLI used its default.
    def pick(param: str, current: Any, key: str) -> Any:
        if ctx.get_parameter_source(param) == click.core.ParameterSource.COMMANDLINE:
            return current
        return section.get(key, current)

    schema = pick("schema", schema, "schema")
    input_data = pick("input_data", input_data, "data")
    template_file = pick("template_file", template_file, "template")
    collection = pick("collection", collection, "collection")
    filename_expr = pick("filename_expr", filename_expr, "filename")
    output_dir = pick("output_dir", output_dir, "output_dir")
    target_class = pick("target_class", target_class, "target_class")
    input_format = pick("input_format", input_format, "input_format")
    validate = pick("validate", validate, "validate")
    typed = pick("typed", typed, "typed")

    # Apply defaults for options that remain unset after config resolution.
    if output_dir is None:
        output_dir = "."
    if validate is None:
        validate = True
    if typed is None:
        typed = True

    missing = [
        name
        for name, value in (
            ("--schema", schema),
            ("--data", input_data),
            ("--template", template_file),
            ("--filename", filename_expr),
        )
        if value in (None, "")
    ]
    if missing:
        raise click.UsageError(f"missing required option(s): {', '.join(missing)} (provide via CLI or --config)")

    if not Path(template_file).is_file():
        raise click.BadParameter(f"template file does not exist: '{template_file}'")

    # Variables: config `vars` (incl. target generator_args) first, then --var overrides.
    template_vars = _config_vars(config, target, section)
    template_vars.update(_parse_template_vars(template_vars_raw))

    for path in render_file(
        schema=schema,
        data_path=input_data,
        template_file=template_file,
        filename_expr=filename_expr,
        output_dir=output_dir,
        collection=collection,
        target_class=target_class,
        input_format=input_format,
        validate=validate,
        typed=typed,
        template_vars=template_vars,
        config=config,
    ):
        click.echo(f"wrote {path}")


if __name__ == "__main__":
    cli()
