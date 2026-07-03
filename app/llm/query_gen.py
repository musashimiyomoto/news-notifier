from app.config import get_settings
from app.llm.client import OllamaClient
from app.llm.schemas import QUERY_GEN_SCHEMA

SYSTEM_PROMPT = (
    "You generate concise web search queries in English to find recent news relevant "
    "to a prediction market's resolution question. Cover different angles: official "
    "statements/data releases, breaking events, analyst or expert commentary. Keep each "
    "query short and search-engine-friendly, not a full sentence."
)


async def generate_queries(market_description: str) -> list[str]:
    settings = get_settings()
    client = OllamaClient()
    result = await client.generate_json(
        model=settings.llm_query_gen_model,
        system=SYSTEM_PROMPT,
        prompt=f"Prediction market resolution question:\n{market_description}\n\nGenerate search queries.",
        schema=QUERY_GEN_SCHEMA,
    )
    queries = result.get("queries") or []
    return [q for q in queries if q.strip()][:5]
