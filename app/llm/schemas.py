"""JSON schemas passed as the `response_format.json_schema.schema` param (see
app.llm.client.LLMClient) for strict structured-output decoding.

NOTE: property ORDER matters. llama.cpp's grammar-constrained decoding emits
required properties in the order they're declared here, and an autoregressive
model conditions later fields on earlier ones — so each *_reasoning field is
deliberately placed BEFORE its score, making the model justify first and then
score consistently with its own justification (scores written before the
reasoning are systematically worse calibrated on small local models)."""

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
    "additionalProperties": False,
}

EXTRACTION_SCHEMA = {
    "type": "object",
    "properties": {
        "is_relevant": {"type": "boolean"},
        "relevance_reasoning": {"type": "string"},
        "relevance_score": {"type": "number"},
        "credibility_reasoning": {"type": "string"},
        "credibility_signal": {"type": "number"},
        "impact_hint": {
            "type": "string",
            "enum": ["supports_yes", "supports_no", "neutral", "ambiguous"],
        },
        "title": {"type": "string"},
        "summary": {"type": "string"},
        "proofs": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"quote": {"type": "string"}},
                "required": ["quote"],
                "additionalProperties": False,
            },
            "maxItems": 3,
        },
    },
    "required": [
        "is_relevant",
        "relevance_reasoning",
        "relevance_score",
        "credibility_reasoning",
        "credibility_signal",
        "impact_hint",
        "title",
        "summary",
        "proofs",
    ],
    "additionalProperties": False,
}
