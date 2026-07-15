from unittest import mock

from app.llm import query_gen


def _patch_llm(response: dict, captured: dict):
    async def fake_generate_json(self, **kwargs):
        captured.update(kwargs)
        return response

    return mock.patch.object(query_gen.LLMClient, "generate_json", fake_generate_json)


async def test_queries_are_deduped_case_insensitively_and_capped_at_five():
    captured = {}
    response = {
        "queries": [
            "Fed rate decision",
            "fed  rate decision",   # dup modulo case/whitespace
            "   ",                  # blank
            "FOMC September meeting",
            "Powell statement",
            "inflation data release",
            "rate cut odds analysts",
            "one query too many",
        ]
    }
    with _patch_llm(response, captured):
        queries = await query_gen.generate_queries("Will the Fed cut rates in September?")

    assert queries[0] == "Fed rate decision"
    assert "fed  rate decision" not in queries
    assert len(queries) == 5


async def test_prompt_carries_market_description_and_todays_date():
    captured = {}
    with _patch_llm({"queries": ["a"]}, captured):
        await query_gen.generate_queries("Will X happen?")

    assert "Will X happen?" in captured["prompt"]
    assert "Today's date:" in captured["prompt"]
    # Query gen deliberately runs hotter than extraction for lexical variety.
    assert captured["temperature"] == 0.7


async def test_missing_queries_key_yields_empty_list():
    with _patch_llm({"queries": None}, {}):
        assert await query_gen.generate_queries("desc") == []
