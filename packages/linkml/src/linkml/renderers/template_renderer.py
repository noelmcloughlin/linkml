"""Render LinkML instance data through a Jinja2 template (``linkml-render``).

This is the instance-data counterpart of the schema-driven ``gen-*`` generators:
it loads an instance-data file (validated against a schema), then renders each
item of a chosen collection through a user-supplied Jinja2 template, writing one
output file per item. All target-language/format specifics live in the template,
so the same engine can emit Java, docs, SQL, config, etc.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from jinja2 import Environment, FileSystemLoader, StrictUndefined, Template, TemplateSyntaxError

from linkml.generators.pythongen import PythonGenerator
from linkml.utils.datautils import _get_format, _is_rdf_format, get_loader, infer_root_class
from linkml.validator import validate as run_validation
from linkml_runtime.dumpers import json_dumper
from linkml_runtime.utils.formatutils import camelcase
from linkml_runtime.utils.schemaview import SchemaView

_DICT_MODE_FORMATS = {"yaml", "yml", "json", "json-ld"}


class TemplateRenderer:
    """Load LinkML instance data and render it through Jinja2 templates.

    :param schema: Path to (or in-memory) LinkML schema.
    :param typed: When True (default) instance data is loaded into typed
        LinkML-runtime objects (applying defaults/inference); when False it is
        loaded as plain dicts (faster, YAML/JSON only, no default inference).
    """

    def __init__(self, schema: str, typed: bool = True):
        self.schema = schema
        self.schemaview = SchemaView(schema)
        self.typed = typed
        self._python_module = None
        self.root_class: str | None = None

    @property
    def python_module(self):
        """The compiled Python data model (built lazily, typed mode only)."""
        if self._python_module is None:
            self._python_module = PythonGenerator(self.schema).compile_module()
        return self._python_module

    def _python_class(self, class_name: str):
        module = self.python_module
        for candidate in (class_name, camelcase(class_name)):
            if candidate in module.__dict__:
                return module.__dict__[candidate]
        raise ValueError(f"could not find a generated Python class for root class '{class_name}'")

    def _resolve_root_class(self, target_class: str | None) -> str:
        if target_class is None:
            target_class = infer_root_class(self.schemaview)
        if target_class is None:
            raise ValueError(
                "Target class not specified and could not be inferred; "
                "pass target_class= or add `tree_root: true` to a class."
            )
        if target_class not in self.schemaview.all_classes():
            raise ValueError(f"target class '{target_class}' is not defined in the schema")
        return target_class

    def load(
        self,
        data_path: str,
        target_class: str | None = None,
        input_format: str | None = None,
        validate: bool = True,
    ):
        """Load and (optionally) validate the root instance object.

        :param data_path: Path to the instance-data file.
        :param target_class: Root class name; inferred (tree_root) when omitted.
        :param input_format: Data format; inferred from the file suffix when omitted.
        :param validate: Validate the data against the schema before rendering.
        :return: The loaded root object (typed instance, or dict in non-typed mode).
        """
        target_class = self._resolve_root_class(target_class)
        self.root_class = target_class
        input_format = _get_format(data_path, input_format)

        if not self.typed:
            if input_format not in _DICT_MODE_FORMATS:
                raise ValueError(
                    f"non-typed (dict) mode supports {sorted(_DICT_MODE_FORMATS)} only; "
                    f"use typed mode for '{input_format}'"
                )
            with open(data_path, encoding="utf-8") as stream:
                root = yaml.safe_load(stream)
            if validate:
                run_validation(root, self.schema, target_class).raise_for_results()
            return root

        inargs = {}
        if _is_rdf_format(input_format):
            inargs["schemaview"] = self.schemaview
            inargs["fmt"] = input_format
        obj = get_loader(input_format).load(source=data_path, target_class=self._python_class(target_class), **inargs)
        if validate:
            run_validation(json_dumper.to_dict(obj), self.schema, target_class).raise_for_results()
        return obj

    def _collection_items(self, root_obj, collection: str | None) -> list:
        if collection is None:
            return [root_obj]
        if self.root_class is not None:
            slots = self.schemaview.class_slots(self.root_class)
            if collection not in slots:
                raise ValueError(f"collection '{collection}' is not a slot of root class '{self.root_class}'")
            if not self.schemaview.induced_slot(collection, self.root_class).multivalued:
                raise ValueError(f"collection slot '{collection}' must be multivalued")
        value = root_obj.get(collection) if isinstance(root_obj, dict) else getattr(root_obj, collection, None)
        if value is None:
            return []
        if isinstance(value, (str, bytes)) or not isinstance(value, (list, tuple)):
            raise ValueError(f"collection '{collection}' did not yield a list of items")
        return list(value)

    def render(
        self,
        root_obj,
        content_template: Template,
        filename_template: Template,
        collection: str | None = None,
        template_vars: dict[str, Any] | None = None,
        config: dict[str, Any] | None = None,
    ) -> dict[str, str]:
        """Render items to a mapping of ``{relative_path: rendered_text}``.

        :param root_obj: The loaded root instance object.
        :param content_template: Template rendered once per item (context: ``item``, ``root``, ``sv``).
        :param filename_template: Template producing each item's output path (context: ``item``, ``root``).
        :param collection: Name of the root slot to iterate; if omitted, the root
            object itself is rendered once.
        :param template_vars: Optional user-provided variables exposed to templates
            as ``vars``.
        :param config: Optional parsed configuration mapping exposed to templates
            as ``config`` (e.g. a ``gen-project`` ``config.yaml``).
        :raises ValueError: if two items resolve to the same output path.
        """
        results: dict[str, str] = {}
        vars_context = template_vars or {}
        config_context = config or {}
        for item in self._collection_items(root_obj, collection):
            relpath = filename_template.render(
                item=item, root=root_obj, sv=self.schemaview, vars=vars_context, config=config_context
            ).strip()
            if not relpath:
                raise ValueError("filename template produced an empty path for an item")
            if relpath in results:
                raise ValueError(f"duplicate output path '{relpath}' produced by multiple items")
            results[relpath] = content_template.render(
                item=item, root=root_obj, sv=self.schemaview, vars=vars_context, config=config_context
            )
        return results

    @staticmethod
    def write(results: dict[str, str], output_dir: str | Path) -> list[Path]:
        """Write rendered results under ``output_dir``, refusing paths that escape it."""
        base = Path(output_dir).resolve()
        written: list[Path] = []
        for relpath, text in results.items():
            target = (base / relpath).resolve()
            if target != base and not target.is_relative_to(base):
                raise ValueError(f"refusing to write outside the output directory: '{relpath}'")
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text, encoding="utf-8")
            written.append(target)
        return written

    @staticmethod
    def environment(template_dir: str | Path | None = None) -> Environment:
        """Build a Jinja2 environment (StrictUndefined, trailing newline kept)."""
        loader = FileSystemLoader(str(template_dir)) if template_dir is not None else None
        return Environment(
            loader=loader,
            undefined=StrictUndefined,
            keep_trailing_newline=True,
            autoescape=False,
        )


def render_file(
    schema: str,
    data_path: str,
    template_file: str,
    filename_expr: str,
    output_dir: str | Path,
    collection: str | None = None,
    target_class: str | None = None,
    input_format: str | None = None,
    validate: bool = True,
    typed: bool = True,
    template_vars: dict[str, Any] | None = None,
    config: dict[str, Any] | None = None,
) -> list[Path]:
    """Convenience one-shot: load, render, and write to ``output_dir``."""
    renderer = TemplateRenderer(schema, typed=typed)
    root = renderer.load(data_path, target_class=target_class, input_format=input_format, validate=validate)
    template_path = Path(template_file)
    env = renderer.environment(template_path.parent)
    try:
        content_template = env.get_template(template_path.name)
        filename_template = env.from_string(filename_expr)
    except TemplateSyntaxError as e:
        raise ValueError(f"template syntax error in '{template_file}': {e}") from e
    results = renderer.render(
        root,
        content_template,
        filename_template,
        collection=collection,
        template_vars=template_vars,
        config=config,
    )
    return renderer.write(results, output_dir)
