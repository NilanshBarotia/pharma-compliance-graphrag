#!/usr/bin/env python3
"""Generate ontology/ontology.schema.json.

The ontology is defined once here (node labels, properties, allowed
relationships) and emitted as a JSON Schema (draft 2020-12). The generated
schema validates a "graph document":

    {"nodes": [...], "edges": [...]}

Edit NODE_TYPES / RELATIONSHIPS below, then run:  python ontology/build_schema.py
"""
import json
from pathlib import Path

OUT = Path(__file__).with_name("ontology.schema.json")

# label -> (id prefix, extra properties, required extra properties)
S = {"type": "string"}
DATE = {"type": "string", "pattern": r"^\d{4}-\d{2}-\d{2}$"}


def enum(*vals):
    return {"type": "string", "enum": list(vals)}


NODE_TYPES = {
    "Product": ("PRD", {
        "dosage_form": enum("tablet", "capsule", "oral_liquid", "injectable", "topical", "other"),
        "strength": S,
        "active_ingredient": S,
    }, []),
    "Batch": ("BAT", {
        "batch_number": S,
        "manufacture_date": DATE,
        "expiry_date": DATE,
        "status": enum("in_process", "quarantined", "on_hold", "released", "rejected"),
        "batch_size": {"type": "object",
                       "properties": {"value": {"type": "number"}, "unit": S},
                       "required": ["value", "unit"], "additionalProperties": False},
    }, ["batch_number"]),
    "Equipment": ("EQP", {
        "equipment_tag": S,
        "equipment_type": S,
        "status": enum("qualified", "calibration_overdue", "under_maintenance", "out_of_service", "retired"),
        "last_calibration_date": DATE,
        "next_calibration_due": DATE,
    }, ["equipment_tag"]),
    "SOP": ("SOP", {
        "sop_number": S,
        "title": S,
        "version": S,
        "effective_date": DATE,
        "department": S,
    }, ["sop_number", "title"]),
    "Deviation": ("DEV", {
        "deviation_number": S,
        "title": S,
        "category": enum("equipment", "process", "laboratory", "documentation",
                         "computerised_system", "material", "facility", "other"),
        "severity": enum("critical", "major", "minor"),
        "status": enum("open", "under_investigation", "closed"),
        "date_opened": DATE,
        "date_closed": DATE,
        "root_cause": S,
        "summary": S,
    }, ["deviation_number", "title"]),
    "CAPA": ("CAP", {
        "capa_number": S,
        "title": S,
        "capa_type": enum("corrective", "preventive", "corrective_and_preventive"),
        "status": enum("open", "in_progress", "closed"),
        "date_opened": DATE,
        "due_date": DATE,
        "effectiveness_check": enum("pending", "effective", "ineffective", "not_required"),
        "description": S,
    }, ["capa_number", "title"]),
    "RegulationClause": ("REG", {
        "framework": enum("21_CFR_211", "21_CFR_210", "EU_GMP_ANNEX_11", "EU_GMP_ANNEX_15", "EU_GMP_OTHER", "OTHER"),
        "citation": S,
        "title": S,
        "jurisdiction": enum("US", "EU"),
        "url": {"type": "string", "format": "uri"},
        "verification_status": enum("verified_against_source", "unverified"),
    }, ["framework", "citation"]),
    "Site": ("SIT", {
        "site_type": enum("manufacturing", "testing_lab", "packaging", "warehouse", "other"),
        "city": S,
        "country": S,
        "fei_number": {"type": "string", "pattern": r"^\d{7,10}$"},
    }, []),
    "Inspector": ("INS", {
        "inspector_type": enum("regulator", "internal_auditor", "third_party_auditor"),
        "organization": S,
    }, []),
}

# (relationship type, from label, to label, extra edge properties)
RELATIONSHIPS = [
    ("OF_PRODUCT", "Batch", "Product", {}),
    ("MANUFACTURED_AT", "Batch", "Site", {}),
    ("PROCESSED_ON", "Batch", "Equipment", {"step": S}),
    ("GOVERNED_BY", "Batch", "SOP", {}),
    ("LOCATED_AT", "Equipment", "Site", {}),
    ("COVERED_BY", "Equipment", "SOP", {}),
    ("IMPLEMENTS", "SOP", "RegulationClause", {}),
    ("OBSERVED_IN", "Deviation", "Batch", {}),
    ("INVOLVES_EQUIPMENT", "Deviation", "Equipment", {}),
    ("VIOLATES_SOP", "Deviation", "SOP", {}),
    ("OCCURRED_AT", "Deviation", "Site", {}),
    ("RELATES_TO_CLAUSE", "Deviation", "RegulationClause", {}),
    ("RAISED", "Inspector", "Deviation", {"date": DATE}),
    ("ADDRESSES", "CAPA", "Deviation", {}),
    ("REVISES_SOP", "CAPA", "SOP", {}),
    ("REFERENCES_CLAUSE", "CAPA", "RegulationClause", {}),
    ("INSPECTED", "Inspector", "Site", {"start_date": DATE, "end_date": DATE}),
    ("CITED", "Inspector", "RegulationClause",
     {"site_id": {"type": "string", "pattern": r"^SIT-[A-Za-z0-9._-]+$"}, "document_id": S}),
    ("CHILD_OF", "RegulationClause", "RegulationClause", {}),
]

ID_BODY = r"[A-Za-z0-9._()\-]+"


def id_pattern(label):
    return {"type": "string", "pattern": rf"^{NODE_TYPES[label][0]}-{ID_BODY}$"}


def node_def(label):
    prefix, props, req = NODE_TYPES[label]
    return {
        "title": label,
        "type": "object",
        "properties": {
            "label": {"const": label},
            "id": id_pattern(label),
            "name": {"type": "string", "minLength": 1,
                     "description": "Canonical display name (entity resolution target)."},
            "aliases": {"type": "array", "items": S, "uniqueItems": True,
                        "description": "Other strings the corpus uses for this entity."},
            "synthetic": {"type": "boolean",
                          "description": "True for fictional data created for this project."},
            "sources": {"$ref": "#/$defs/sources"},
            **props,
        },
        "required": ["label", "id", "name", "sources", *req],
        "additionalProperties": False,
    }


def edge_def(rel, frm, to, props):
    return {
        "title": f"{frm}-[{rel}]->{to}",
        "type": "object",
        "properties": {
            "type": {"const": rel},
            "from": {"type": "object",
                     "properties": {"label": {"const": frm}, "id": id_pattern(frm)},
                     "required": ["label", "id"], "additionalProperties": False},
            "to": {"type": "object",
                   "properties": {"label": {"const": to}, "id": id_pattern(to)},
                   "required": ["label", "id"], "additionalProperties": False},
            "properties": {"type": "object", "properties": props, "additionalProperties": False},
            "sources": {"$ref": "#/$defs/sources"},
        },
        "required": ["type", "from", "to", "sources"],
        "additionalProperties": False,
    }


def build():
    defs = {
        "sources": {
            "type": "array", "minItems": 1,
            "description": "Provenance. Every node/edge must point at >=1 source document (drives citation grounding).",
            "items": {"type": "object",
                      "properties": {"doc_id": {"type": "string", "pattern": r"^(SRC|SYN)-[A-Za-z0-9._-]+$"},
                                     "locator": {"type": "string",
                                                 "description": "Section, clause, page or paragraph inside the doc."}},
                      "required": ["doc_id"], "additionalProperties": False},
        }
    }       
    for label in NODE_TYPES:
        defs[f"node_{label}"] = node_def(label)
    for i, (rel, frm, to, props) in enumerate(RELATIONSHIPS):
        defs[f"edge_{rel}"] = edge_def(rel, frm, to, props)

    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": "https://example.org/pharma-graphrag/ontology.schema.json",
        "title": "Pharma manufacturing compliance knowledge graph (v0.1)",
        "description": "Graph document: typed nodes + typed, direction-constrained edges. "
                       "Educational project; fictional synthetic data is flagged synthetic=true.",
        "type": "object",
        "properties": {
            "nodes": {"type": "array", "items": {"oneOf": [{"$ref": f"#/$defs/node_{l}"} for l in NODE_TYPES]}},
            "edges": {"type": "array", "items": {"oneOf": [{"$ref": f"#/$defs/edge_{r[0]}"} for r in RELATIONSHIPS]}},
        },
        "required": ["nodes", "edges"],
        "additionalProperties": False,
        "$defs": defs,
    }


if __name__ == "__main__":
    OUT.write_text(json.dumps(build(), indent=2) + "\n")
    print(f"wrote {OUT} ({len(NODE_TYPES)} node types, {len(RELATIONSHIPS)} relationship rules)")
