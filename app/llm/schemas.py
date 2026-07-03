"""JSON schemas passed as Ollama's `format` param for constrained/structured decoding."""

QUERY_GEN_SCHEMA = {
    "type": "object",
    "properties": {
        "queries": {
            "type": "array",
            "items": {"type": "string"},
            "minItems": 2,
            "maxItems": 5,
        }
    },
    "required": ["queries"],
}

EXTRACTION_SCHEMA = {
    "type": "object",
    "properties": {
        "is_relevant": {"type": "boolean"},
        "title": {"type": "string"},
        "summary": {"type": "string"},
        "relevance_score": {"type": "number"},
        "relevance_reasoning": {"type": "string"},
        "credibility_signal": {"type": "number"},
        "credibility_reasoning": {"type": "string"},
        "impact_hint": {
            "type": "string",
            "enum": ["supports_yes", "supports_no", "neutral", "ambiguous"],
        },
        "proofs": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"quote": {"type": "string"}},
                "required": ["quote"],
            },
        },
    },
    "required": [
        "is_relevant",
        "title",
        "summary",
        "relevance_score",
        "relevance_reasoning",
        "credibility_signal",
        "credibility_reasoning",
        "impact_hint",
        "proofs",
    ],
}
