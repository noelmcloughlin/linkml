.. _render:

Rendering data with templates (linkml-render)
=============================================

The ``linkml-render`` command renders **instance data** through a
`Jinja2 <https://jinja.palletsprojects.com/>`__ template, writing one output
file per item of a chosen collection.

Where the ``gen-*`` generators turn a *schema* into an artifact, and
``linkml-convert`` moves *data* between serialization formats,
``linkml-render`` turns *data* into arbitrary text: source code, documentation,
configuration, SQL, and so on. All target-specific logic lives in the template,
so the same engine can emit any text format.

How it works
------------

1. The schema is loaded and (unless ``--no-validate``) the instance data is
   validated against it.
2. The root object is loaded - either as typed LinkML-runtime objects (default)
   or as plain dicts (``--no-typed``, YAML/JSON only, faster).
3. Each item of ``--collection`` (a multivalued slot of the root class) is
   rendered through the template. With no ``--collection`` the root object is
   rendered once.
4. Each rendered file is written under ``--output-dir`` at a path produced by the
   ``--filename`` expression.

Template context:

- ``item`` - the current item being rendered (or the root, if no collection)
- ``root`` - the root object
- ``sv`` - a :class:`SchemaView` over the schema
- ``vars`` - optional user-supplied variables from repeatable ``--var KEY=VALUE``
- ``config`` - the parsed configuration mapping (from ``--config`` or a default
  ``config.yaml``); an empty mapping when no config file is found

Configuration file
------------------

Reuse an existing ``gen-project`` ``config.yaml`` instead of repeating values on
the command line. ``--config`` is optional: when omitted, a ``config.yaml`` in
the current directory is used automatically (as with ``gen-project``). The parsed
file is exposed to templates as ``config``.

Different targets (java, proto, graphql, python, typescript, ...) need different
templates and variables, so key the ``render:`` block by target name (parallel to
``generator_args``) and select one with ``--target``. Each target supplies its own
``template``, ``collection``, ``filename``, ``output_dir`` and ``vars``; the
target's ``generator_args`` entry is merged into ``vars`` (so the java ``package``
is reused, not duplicated). Targets use only the variables they need - java has a
``package``; graphql and python have none.

.. code-block:: yaml

   # config.yaml
   generator_args:
     java:
       package: com.example.mkv    # merged into vars for --target java
   render:
     java:
       template: annotation.java.jinja2
       collection: annotation_types
       filename: "{{ vars.package.replace('.', '/') }}/{{ item.name }}.java"
       output_dir: out/java
     proto:
       template: annotation.proto.jinja2
       collection: annotation_types
       filename: "{{ item.name }}.proto"
       output_dir: out/proto
     graphql:
       template: annotation.graphql.jinja2
       collection: annotation_types
       filename: "{{ item.name }}.graphql"
       output_dir: out/graphql
     python:
       template: annotation.python.jinja2
       collection: annotation_types
       filename: "{{ item.name }}.py"
       output_dir: out/python
     typescript:
       template: annotation.typescript.jinja2
       collection: annotation_types
       filename: "{{ item.name }}.ts"
       output_dir: out/typescript

Each target is then a one-liner - swap ``--target`` (nothing in the engine is
language-specific; the template alone determines the output):

.. code-block:: bash

   linkml-render -s annotation_catalog_schema.yaml --data annotation_catalog_data.yaml --target java
   linkml-render -s annotation_catalog_schema.yaml --data annotation_catalog_data.yaml --target proto
   # ... likewise: graphql, python, typescript

Explicit command-line options override the config, and ``--var KEY=VALUE``
overrides a target's ``vars``. Precedence, highest to lowest: CLI options and
``--var``; the selected ``render.<target>`` block (with its ``vars``); the
target's ``generator_args`` entry; built-in defaults.

Example
-------

The bundled example renders one *annotation catalog* to several targets by
swapping only the template. The files live under
``tests/linkml/test_generators/input/``: schema
``annotation_catalog_schema.yaml``, data ``annotation_catalog_data.yaml``, and one
``annotation.<target>.jinja2`` template per target (java, graphql, proto, python,
typescript). Each ``annotation_types`` item has ``name``, ``package``,
``retention``, ``targets`` and ``elements``; only the template differs. Render a
target with:

.. code-block:: bash

   linkml-render -s annotation_catalog_schema.yaml --data annotation_catalog_data.yaml \
       --template annotation.java.jinja2 --collection annotation_types \
       --filename "{{ item.package.replace('.', '/') }}/{{ item.name }}.java" -d out/

The same data produces, per template:

**Java** (``@interface``):

.. code-block:: java

   @Retention(RetentionPolicy.RUNTIME)
   @Target({ElementType.FIELD, ElementType.METHOD})
   public @interface MkvField {
     String name() default "";
     boolean useCamelCasing() default true;
     boolean optional() default false;
   }

**GraphQL** (``directive``):

.. code-block:: graphql

   """Optional annotation used to specify attribute mapping overrides."""
   directive @MkvField(
     name: String = ""
     useCamelCasing: Boolean = true
     optional: Boolean = false
   ) on FIELD_DEFINITION

**Protobuf** (custom option):

.. code-block:: protobuf

   message MkvField {
     string name = 1;
     bool useCamelCasing = 2;
     bool optional = 3;
   }

   extend google.protobuf.FieldOptions {
     MkvField mkvfield = 50001;
   }
   extend google.protobuf.MethodOptions {
     MkvField mkvfield = 50001;
   }

**Python** (decorator factory):

.. code-block:: python

   def MkvField(name="", useCamelCasing=True, optional=False):
       def _decorator(target):
           target.__MkvField__ = {
               "name": name,
               "useCamelCasing": useCamelCasing,
               "optional": optional,
           }
           return target
       return _decorator

**TypeScript** (decorator factory):

.. code-block:: typescript

   /** Optional annotation used to specify attribute mapping overrides. */
   export function MkvField(name: string = "", useCamelCasing: boolean = true, optional: boolean = false) {
     return function (target: any) {
       (target as any).__MkvField__ = { name, useCamelCasing, optional };
       return target;
     };
   }


Notes
-----

- **Templates are strict.** An undefined variable raises an error
  (``StrictUndefined``) so template typos are caught early. In ``--no-typed``
  mode use ``x is defined`` / ``x.get(...)`` for optional keys; in typed mode
  every slot is an attribute (absent values are ``None``).
- **Includes** are supported: the template's directory is on the Jinja search
  path, so ``{% include "partial.jinja2" %}`` works.
- **Output paths are sandboxed.** A ``--filename`` that resolves outside
  ``--output-dir`` (absolute paths or ``..``) is rejected.

Command Line
------------

.. click:: linkml.renderers.cli:cli
    :prog: linkml-render
    :nested: full
