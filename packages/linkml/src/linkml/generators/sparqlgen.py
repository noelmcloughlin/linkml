import logging
import os
import re
from collections import defaultdict
from dataclasses import dataclass
from functools import partial
from pathlib import Path

import click
from jinja2 import Template

from linkml._version import __version__
from linkml.utils.generator import Generator, shared_arguments
from linkml_runtime.linkml_model.meta import Prefix, SlotDefinition
from linkml_runtime.utils.formatutils import underscore
from linkml_runtime.utils.schemaview import SchemaView

logger = logging.getLogger(__name__)

# The local names that a SPARQL prefixed name accepts with no escaping, kept to ASCII. Any other term
# is written as a full IRI, which is always accepted.
SPARQL_LOCAL_NAME = re.compile(r"\w(?:[\w.-]*[\w-])?", re.ASCII)

template = """
{% for pfxn, pfx in prefixes.items() -%}
PREFIX {{pfxn}}: <{{pfx.prefix_reference}}>
{% endfor %}

{% for cn, c in schema_view.all_classes(imports=imports).items() if not c.mixin and not c.abstract %}

## --
## Checks for {{ cn }}
## --

# @CHECK permitted_{{cn}}
SELECT ?g ?s ?p WHERE {
 GRAPH ?g {
  ?s rdf:type {{ term(schema_view.get_uri(cn)) }} ;
     ?p ?o .
  FILTER ( ?p NOT IN (
   {% for slot in schema_view.class_induced_slots(cn) -%}
   {{ term(slot_uri(schema_view, cn, slot)) }},
   {% endfor -%}
   rdf:type ))
 }
 {{ extra }}
} {{ limit }}


{% for slot in schema_view.class_induced_slots(cn) -%}

{% if slot.required %}
# @CHECK required_{{cn}}_{{slot.name}}
SELECT
  ?check
  ?graph
  ?subject
  ?predicate WHERE {
 GRAPH ?graph {
  ?subject rdf:type {{ term(schema_view.get_uri(cn)) }} .
  FILTER NOT EXISTS { ?subject  {{ term(slot_uri(schema_view, cn, slot)) }} ?o  }
 }
 VALUES ?check { linkml:required }
 VALUES ?predicate { {{ term(slot_uri(schema_view, cn, slot)) }} }
 {{ extra }}
}  {{ limit }}
{% endif %}

{% if slot.range in schema_view.all_classes() %}
# @CHECK object_range_{{cn}}_{{slot.name}}
SELECT
  ?check
  ?graph
  ?subject
  ?predicate
  ?object
WHERE {
 GRAPH ?graph {
  ?subject rdf:type {{ term(schema_view.get_uri(cn)) }} ;
     ?predicate ?object .
  FILTER NOT EXISTS {
    ?object rdf:type ?otype .
    FILTER ( ?otype IN (
      {% for a in schema_view.class_descendants(slot.range) -%}
      {{ term(schema_view.get_uri(a)) }}
      {{ ", " if not loop.last else "" }}
      {% endfor -%} ))
  }
 }
 VALUES ?check { linkml:range }
 VALUES ?predicate { {{ term(slot_uri(schema_view, cn, slot)) }}  }
 {{ extra }}
}  {{ limit }}
{% endif %}

{%- endfor %}


## -- End of checks for {{ cn }}
{% endfor %}
"""

x = """
{% for sn in schema_view.class_slots(c.name) %}
     {{ schema.slots[sn].slot_uri }}
   {% endfor %}
"""


def query_prefixes(schemaview: SchemaView) -> dict[str, Prefix]:
    """Return the prefixes that the queries declare.

    A query can name a class or slot from any imported schema, even when no query is written for
    that class, so the prefixes of every imported schema are included. When two schemas declare the
    same prefix, the schema given to the generator comes first, and then the order of the imports.

    An IRI is declared under one name only, because rdflib forgets the earlier of two names for the
    same IRI and then refuses a query that uses it. ``query_term`` writes a term that uses a name
    left out here as a full IRI.
    """
    prefixes = {}
    for schema in (schemaview.schema, *schemaview.all_schema()):
        for name, prefix in schema.prefixes.items():
            if prefix.prefix_reference not in {declared.prefix_reference for declared in prefixes.values()}:
                prefixes.setdefault(name, prefix)
    prefixes.setdefault("rdf", Prefix("rdf", "http://www.w3.org/1999/02/22-rdf-syntax-ns#"))
    prefixes.setdefault("linkml", Prefix("linkml", "https://w3id.org/linkml/"))
    return prefixes


def query_term(schemaview: SchemaView, prefixes: dict[str, Prefix], uri: str) -> str:
    r"""Write the URI or CURIE of a class or slot as a term of a query.

    A CURIE stays as it is when the queries declare its prefix and SPARQL accepts its local name.
    Anything else is written as a full IRI in angle brackets. That covers a URI declared in full, a
    URI built for a schema with no ``default_prefix``, and a CURIE whose prefix comes only from
    ``default_curi_maps``.

    >>> view = SchemaView("id: https://example.org/s\nname: s\nprefixes:\n  ex: https://example.org/\n")
    >>> prefixes = query_prefixes(view)
    >>> query_term(view, prefixes, "ex:name")
    'ex:name'
    >>> query_term(view, prefixes, "ex:a/b")
    '<https://example.org/a/b>'
    >>> query_term(view, prefixes, "https://schema.org/name")
    '<https://schema.org/name>'
    """
    prefix, separator, local_name = uri.partition(":")
    if separator and prefix in prefixes and SPARQL_LOCAL_NAME.fullmatch(local_name):
        return uri
    return f"<{schemaview.expand_curie(uri)}>"


def slot_uri(schemaview: SchemaView, class_name: str, slot: SlotDefinition) -> str:
    """Return the URI of an induced slot of a class.

    A slot with no declared URI takes the default prefix of the schema that defines it. An induced
    slot cannot name that schema, because a ``slot_usage`` in another schema replaces its
    ``from_schema``, so this function finds the definition again. An attribute of the class or of an
    ancestor comes before a schema-level slot of the same name, as in ``SchemaView.induced_slot``.
    """
    if slot.slot_uri:
        return slot.slot_uri
    for ancestor in schemaview.class_ancestors(class_name):
        attributes = schemaview.get_class(ancestor).attributes
        if slot.name in attributes:
            return schemaview.get_uri(attributes[slot.name])
    return schemaview.get_uri(schemaview.get_slot(slot.name, attributes=False))


@dataclass
class SparqlGenerator(Generator):
    """
    Generates SPARQL queries that can be used for delayed validation
    """

    # ClassVars
    generatorname = os.path.basename(__file__)
    valid_formats = ["sparql"]
    visit_all_class_slots = False
    uses_schemaloader = False

    # ObjectVars
    named_graphs: list[str] | None = None
    limit: int | None = None
    sparql: str | None = None

    def __post_init__(self):
        super().__post_init__()
        # No copy of the view is needed here. #3778 added one because materialize_schema rewrote the
        # schema, which corrupted the input of any generator run afterwards (#3770). Adding
        # --no-mergeimports then showed that the rewrite itself gave wrong queries: moving attributes
        # into the root schema changed the prefix of inherited ones, and attributes sharing a name
        # overwrote each other. So the rewrite was removed, and query_prefixes and slot_uri read the
        # same facts from the view, which leaves a copy nothing to protect. The one write left is
        # domain_of, which SchemaView.induced_slot fills in for any reader of a view, including the
        # other SchemaView generators.
        self.queries = self.generate_sparql(named_graphs=self.named_graphs, limit=self.limit)

    def generate_sparql(self, named_graphs=None, limit: int = None):
        template_obj = Template(template)
        extra = ""
        if named_graphs is not None:
            extra += f"FILTER( ?graph in ( {','.join(named_graphs)} ))"
        logger.info(f"Named Graphs = {named_graphs} // extra={extra}")
        if limit is not None and isinstance(limit, int):
            limit = f"LIMIT {limit}"
        else:
            limit = ""
        prefixes = query_prefixes(self.schemaview)
        sparql = template_obj.render(
            schema_view=self.schemaview,
            prefixes=prefixes,
            term=partial(query_term, self.schemaview, prefixes),
            slot_uri=slot_uri,
            limit=limit,
            extra=extra,
            imports=bool(self.mergeimports),
        )
        self.sparql = sparql
        queries = self.split_sparql(sparql)
        return queries

    def serialize(self, directory=None) -> str:
        if directory is not None:
            Path(directory).mkdir(parents=True, exist_ok=True)
            for qn, q in self.queries.items():
                qpath = os.path.join(directory, f"{qn}.rq")
                with open(qpath, "w", encoding="UTF-8") as stream:
                    stream.write(q)
        return self.sparql

    @staticmethod
    def split_sparql(sparql: str) -> dict[str, str]:
        lines = sparql.split("\n")
        prolog = ""
        queries = defaultdict(str)
        q = None
        for line in lines:
            if line.startswith("# @"):
                q = underscore(line.replace("# @", ""))
                queries[q] = prolog + "\n"
            elif q is None:
                if line.lower().startswith("prefix"):
                    prolog += line + "\n"
            else:
                queries[q] += line + "\n"
        return queries


@shared_arguments(SparqlGenerator)
@click.command(name="sparql")
@click.option("--dir", "-d", help="Directory in which queries will be deposited")
@click.version_option(__version__, "-V", "--version")
def cli(yamlfile, dir, **kwargs):
    """Generate SPARQL queries for validation

    This will generate a directory of queries that can be used for QC over a triplestore that
    is conformant to the same LinkML schema.

    Each query in the directory will be of the form

        CHECK_<ConstraintType>_<SchemaElement>.rq

    Example:

        gen-sparql -d ./sparql/ personinfo.yaml
    """
    SparqlGenerator(yamlfile, **kwargs).serialize(directory=dir)


if __name__ == "__main__":
    cli()
