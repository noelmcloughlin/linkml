import pytest

from linkml.renderers.template_renderer import TemplateRenderer, render_file


def _write_template(tmp_path, body, name="t.jinja2"):
    template = tmp_path / name
    template.write_text(body, encoding="utf-8")
    return str(template)


@pytest.fixture
def schema_path(input_path):
    return input_path("render_catalog_schema.yaml")


@pytest.fixture
def data_path(input_path):
    return input_path("render_catalog_data.yaml")


def test_render_file_one_per_item(schema_path, data_path, tmp_path):
    """render_file writes one file per item, at a path derived from the item's data."""
    template = tmp_path / "item.txt.jinja2"
    template.write_text("name={{ item.name }} label={{ item.label }}\n", encoding="utf-8")

    written = render_file(
        schema=schema_path,
        data_path=data_path,
        template_file=str(template),
        filename_expr="{{ item.package.replace('.', '/') }}/{{ item.name }}.txt",
        output_dir=str(tmp_path / "out"),
        collection="items",
    )

    assert len(written) == 2
    alpha = tmp_path / "out" / "com" / "example" / "a" / "Alpha.txt"
    beta = tmp_path / "out" / "com" / "example" / "b" / "Beta.txt"
    assert alpha.read_text() == "name=Alpha label=first\n"
    assert beta.read_text() == "name=Beta label=second\n"


def test_render_without_collection_renders_root(schema_path, data_path, tmp_path):
    """With no collection, the root object is rendered once."""
    renderer = TemplateRenderer(schema_path)
    root = renderer.load(data_path)

    from jinja2 import Template

    results = renderer.render(
        root,
        Template("count={{ root.items | length }}"),
        Template("summary.txt"),
        collection=None,
    )
    assert results == {"summary.txt": "count=2"}


def test_render_validates_by_default(schema_path, tmp_path):
    """Invalid data (unknown slot) is rejected before rendering."""
    bad = tmp_path / "bad.yaml"
    bad.write_text("items:\n  - name: X\n    not_a_slot: y\n", encoding="utf-8")
    template = tmp_path / "t.jinja2"
    template.write_text("{{ item.name }}", encoding="utf-8")

    with pytest.raises(Exception):
        render_file(
            schema=schema_path,
            data_path=str(bad),
            template_file=str(template),
            filename_expr="{{ item.name }}.txt",
            output_dir=str(tmp_path / "out"),
            collection="items",
        )


def test_render_rejects_path_traversal(schema_path, data_path, tmp_path):
    """A filename expression that escapes the output dir is rejected."""
    template = _write_template(tmp_path, "{{ item.name }}")
    with pytest.raises(ValueError, match="outside the output directory"):
        render_file(
            schema=schema_path,
            data_path=data_path,
            template_file=template,
            filename_expr="../../{{ item.name }}.txt",
            output_dir=str(tmp_path / "out"),
            collection="items",
        )


def test_render_rejects_absolute_path(schema_path, data_path, tmp_path):
    """An absolute filename path is rejected as escaping the output dir."""
    template = _write_template(tmp_path, "{{ item.name }}")
    with pytest.raises(ValueError, match="outside the output directory"):
        render_file(
            schema=schema_path,
            data_path=data_path,
            template_file=template,
            filename_expr="/tmp/evil/{{ item.name }}.txt",
            output_dir=str(tmp_path / "out"),
            collection="items",
        )


def test_render_detects_duplicate_paths(schema_path, data_path, tmp_path):
    """Two items resolving to the same output path raise an error."""
    template = _write_template(tmp_path, "{{ item.name }}")
    with pytest.raises(ValueError, match="duplicate output path"):
        render_file(
            schema=schema_path,
            data_path=data_path,
            template_file=template,
            filename_expr="same.txt",
            output_dir=str(tmp_path / "out"),
            collection="items",
        )


def test_render_rejects_unknown_collection(schema_path, data_path):
    """An unknown collection slot raises a clear error."""
    renderer = TemplateRenderer(schema_path)
    root = renderer.load(data_path)
    from jinja2 import Template

    with pytest.raises(ValueError, match="is not a slot of root class"):
        renderer.render(root, Template("x"), Template("f.txt"), collection="nope")


def test_render_rejects_non_multivalued_collection(input_path, tmp_path):
    """A single-valued collection slot is rejected."""
    schema = tmp_path / "s.yaml"
    schema.write_text(
        """
id: https://example.org/s
name: s
prefixes:
  linkml: https://w3id.org/linkml/
  s: https://example.org/s/
default_prefix: s
imports: ["linkml:types"]
default_range: string
classes:
  Root:
    tree_root: true
    attributes:
      only:
        range: string
""",
        encoding="utf-8",
    )
    data = tmp_path / "d.yaml"
    data.write_text("only: hi\n", encoding="utf-8")
    renderer = TemplateRenderer(str(schema))
    root = renderer.load(str(data))
    from jinja2 import Template

    with pytest.raises(ValueError, match="must be multivalued"):
        renderer.render(root, Template("x"), Template("f.txt"), collection="only")


def test_render_strict_undefined(schema_path, data_path, tmp_path):
    """Referencing an undefined variable in a template is an error (StrictUndefined)."""
    template = _write_template(tmp_path, "{{ item.does_not_exist_key.foo }}")
    with pytest.raises(Exception):
        render_file(
            schema=schema_path,
            data_path=data_path,
            template_file=template,
            filename_expr="{{ item.name }}.txt",
            output_dir=str(tmp_path / "out"),
            collection="items",
            typed=False,  # in dict mode, an unknown key is StrictUndefined
        )


def test_render_dict_mode(schema_path, data_path, tmp_path):
    """Non-typed (dict) mode renders without compiling the Python model."""
    template = _write_template(tmp_path, "name={{ item.name }}\n")
    written = render_file(
        schema=schema_path,
        data_path=data_path,
        template_file=template,
        filename_expr="{{ item.name }}.txt",
        output_dir=str(tmp_path / "out"),
        collection="items",
        typed=False,
    )
    assert len(written) == 2
    assert (tmp_path / "out" / "Alpha.txt").read_text() == "name=Alpha\n"


def test_cli_smoke(schema_path, data_path, tmp_path):
    """The linkml-render CLI renders one file per item."""
    from click.testing import CliRunner

    from linkml.renderers.cli import cli

    template = _write_template(tmp_path, "name={{ item.name }}\n")
    result = CliRunner().invoke(
        cli,
        [
            "-s", schema_path,
            "--data", data_path,
            "--template", template,
            "--collection", "items",
            "--filename", "{{ item.name }}.txt",
            "-d", str(tmp_path / "out"),
        ],
    )
    assert result.exit_code == 0, result.output
    assert (tmp_path / "out" / "Alpha.txt").read_text() == "name=Alpha\n"


def test_example_render_java_annotations(input_path, tmp_path):
    """The bundled example renders Java @interface files from an annotation catalog."""
    written = render_file(
        schema=input_path("annotation_catalog.yaml"),
        data_path=input_path("annotation_catalog_data.yaml"),
        template_file=input_path("annotation.java.jinja2"),
        filename_expr="{{ item.package.replace('.', '/') }}/{{ item.name }}.java",
        output_dir=str(tmp_path / "java"),
        collection="annotation_types",
    )
    assert len(written) == 2
    field = (tmp_path / "java" / "com" / "example" / "mkv" / "MkvField.java").read_text()
    assert "public @interface MkvField {" in field
    assert "@Retention(RetentionPolicy.RUNTIME)" in field
    assert "@Target({ElementType.FIELD, ElementType.METHOD})" in field
    assert 'String name() default "";' in field
    assert "boolean useCamelCasing() default true;" in field

    typ = (tmp_path / "java" / "com" / "example" / "mkv" / "MkvType.java").read_text()
    assert "@Target(ElementType.TYPE)" in typ  # a single target has no braces


def test_example_render_graphql_directives(input_path, tmp_path):
    """The same catalog renders GraphQL directive definitions through a different template."""
    written = render_file(
        schema=input_path("annotation_catalog.yaml"),
        data_path=input_path("annotation_catalog_data.yaml"),
        template_file=input_path("annotation.graphql.jinja2"),
        filename_expr="{{ item.name }}.graphql",
        output_dir=str(tmp_path / "graphql"),
        collection="annotation_types",
    )
    assert len(written) == 2
    field = (tmp_path / "graphql" / "MkvField.graphql").read_text()
    assert "directive @MkvField(" in field
    assert "name: String = \"\"" in field
    assert "useCamelCasing: Boolean = true" in field
    # FIELD + METHOD both map to FIELD_DEFINITION and are de-duplicated
    assert "on FIELD_DEFINITION" in field

    typ = (tmp_path / "graphql" / "MkvType.graphql").read_text()
    assert "directive @MkvType(" in typ
    assert "on OBJECT" in typ


def test_example_render_protobuf_options(input_path, tmp_path):
    """The catalog renders Protobuf custom-option definitions."""
    written = render_file(
        schema=input_path("annotation_catalog.yaml"),
        data_path=input_path("annotation_catalog_data.yaml"),
        template_file=input_path("annotation.proto.jinja2"),
        filename_expr="{{ item.name }}.proto",
        output_dir=str(tmp_path / "proto"),
        collection="annotation_types",
    )
    assert len(written) == 2
    field = (tmp_path / "proto" / "MkvField.proto").read_text()
    assert 'syntax = "proto3";' in field
    assert "message MkvField {" in field
    assert "string name = 1;" in field
    assert "bool useCamelCasing = 2;" in field
    # FIELD maps to FieldOptions and METHOD to MethodOptions, and both are emitted
    assert "extend google.protobuf.FieldOptions {" in field
    assert "extend google.protobuf.MethodOptions {" in field

    typ = (tmp_path / "proto" / "MkvType.proto").read_text()
    assert "extend google.protobuf.MessageOptions {" in typ


def test_example_render_python_decorator(input_path, tmp_path):
    """The catalog renders Python decorator factories (and they are valid Python)."""
    written = render_file(
        schema=input_path("annotation_catalog.yaml"),
        data_path=input_path("annotation_catalog_data.yaml"),
        template_file=input_path("annotation.python.jinja2"),
        filename_expr="{{ item.name }}.py",
        output_dir=str(tmp_path / "python"),
        collection="annotation_types",
    )
    assert len(written) == 2
    field = (tmp_path / "python" / "MkvField.py").read_text()
    assert "def MkvField(name=\"\", useCamelCasing=True, optional=False):" in field
    assert 'target.__MkvField__ = {' in field
    # generated code must be syntactically valid Python
    compile(field, "MkvField.py", "exec")
    compile((tmp_path / "python" / "MkvType.py").read_text(), "MkvType.py", "exec")
