"""JSON schemas that constrain every agent's output.

Schema-constrained decoding is the reason this pipeline has no "failed to parse
model output" path: the model is structurally unable to return prose where we
expect a warehouse id.
"""
from __future__ import annotations

ORDER_INTAKE = {
    "type": "OBJECT",
    "properties": {
        "lines": {
            "type": "ARRAY",
            "items": {
                "type": "OBJECT",
                "properties": {
                    "sku": {"type": "STRING", "description": "Exact SKU from the catalogue"},
                    "qty": {"type": "INTEGER"},
                },
                "required": ["sku", "qty"],
            },
        },
        "destination": {"type": "STRING", "description": "Full delivery address as stated"},
        "priority": {"type": "STRING", "enum": ["standard", "express"]},
        "cold_chain": {"type": "BOOLEAN"},
        "customer_note": {"type": "STRING"},
        "unmatched_requests": {
            "type": "ARRAY",
            "items": {"type": "STRING"},
            "description": "Requested items with no matching SKU",
        },
        "confidence": {"type": "NUMBER"},
        "reasoning": {"type": "STRING", "description": "One sentence, max 25 words"},
    },
    "required": ["lines", "destination", "priority", "confidence", "reasoning"],
}

FULFILLMENT_PLAN = {
    "type": "OBJECT",
    "properties": {
        "warehouse_id": {"type": "STRING"},
        "carrier": {"type": "STRING"},
        "rationale": {"type": "STRING", "description": "Max 40 words. Cite the numbers you used."},
        "risk_flags": {"type": "ARRAY", "items": {"type": "STRING"}},
        "applied_policies": {
            "type": "ARRAY",
            "items": {"type": "STRING"},
            "description": "Keys of learned policies you relied on",
        },
        "confidence": {"type": "NUMBER"},
    },
    "required": ["warehouse_id", "carrier", "rationale", "confidence"],
}

REPAIR_PLAN = {
    "type": "OBJECT",
    "properties": {
        "strategy": {
            "type": "STRING",
            "enum": ["reroute_warehouse", "switch_carrier", "split_shipment",
                     "downgrade_priority", "partial_fulfil", "escalate_human"],
        },
        "warehouse_id": {"type": "STRING"},
        "carrier": {"type": "STRING"},
        "drop_skus": {"type": "ARRAY", "items": {"type": "STRING"}},
        "explanation": {"type": "STRING", "description": "Max 40 words"},
        "confidence": {"type": "NUMBER"},
    },
    "required": ["strategy", "explanation", "confidence"],
}

PACKAGE_QA = {
    "type": "OBJECT",
    "properties": {
        "verdict": {"type": "STRING", "enum": ["pass", "repack", "reject"]},
        "damage_detected": {"type": "BOOLEAN"},
        "damage_types": {"type": "ARRAY", "items": {"type": "STRING"}},
        "label_readable": {"type": "BOOLEAN"},
        "extracted_text": {"type": "STRING", "description": "Any text/barcode visible on the label"},
        "severity": {"type": "NUMBER", "description": "0.0 pristine to 1.0 destroyed"},
        "notes": {"type": "STRING", "description": "Max 30 words"},
    },
    "required": ["verdict", "damage_detected", "label_readable", "severity", "notes"],
}

FEEDBACK_INSIGHT = {
    "type": "OBJECT",
    "properties": {
        "insights": {
            "type": "ARRAY",
            "items": {
                "type": "OBJECT",
                "properties": {
                    "policy_key": {"type": "STRING", "description": "stable snake_case key"},
                    "rule": {"type": "STRING", "description": "Actionable rule, max 25 words"},
                    "confidence": {"type": "NUMBER"},
                },
                "required": ["policy_key", "rule", "confidence"],
            },
        },
        "summary": {"type": "STRING", "description": "Max 35 words"},
    },
    "required": ["insights", "summary"],
}
