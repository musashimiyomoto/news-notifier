from datetime import datetime, timezone

from app.config import get_settings
from app.llm.client import LLMClient
from app.llm.schemas import QUERY_GEN_SCHEMA

SYSTEM_PROMPT = (
    "You generate concise web search queries in English to find recent news relevant "
    "to a prediction market's resolution question. Rules:\n"
    "- Each query must target a DIFFERENT angle: the official actor/body itself "
    "(statements, data releases, scheduled decisions), the concrete event outcome, "
    "and expert/analyst coverage.\n"
    "- Name the specific entities, not the market framing: search engines index "
    "news about 'Federal Reserve rate decision', not 'will market resolve yes'. "
    "Never include words like 'prediction market', 'resolves', 'odds' in a query.\n"
    "- 2-6 words per query, keyword style, no full sentences, no quotes/operators.\n"
    "- Prefer terms a journalist would put in a headline TODAY (use the current "
    "date to pick the right month/meeting/event names)."
)


async def generate_queries(market_description: str) -> list[str]:
    settings = get_settings()
    client = LLMClient()
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    result = await client.generate_json(
        model=settings.llm_query_gen_model,
        system=SYSTEM_PROMPT,
        prompt=(
            f"Today's date: {today}\n\n"
            f"Prediction market resolution question:\n{market_description}\n\n"
            "Generate search queries."
        ),
        schema=QUERY_GEN_SCHEMA,
        name="query_gen",
        # Higher than extraction's default: query gen benefits from lexical
        # variety across the angles (dedup downstream is cheap), while
        # extraction/scoring wants determinism.
        temperature=0.7,
    )
    queries = result.get("queries") or []
    # Dedup case-insensitively while preserving order — a small model sometimes
    # returns the same query twice with different capitalization, and each
    # duplicate costs three redundant source searches downstream.
    seen: set[str] = set()
    unique = []
    for query in queries:
        normalized = " ".join(query.split()).casefold()
        if normalized and normalized not in seen:
            seen.add(normalized)
            unique.append(query.strip())
    return unique[:5]
