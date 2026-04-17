import logging
import os

import httpx

logger = logging.getLogger(__name__)

_TAVILY_URL = "https://api.tavily.com/search"

WEB_SEARCH_SCHEMA = {
    "type": "object",
    "properties": {
        "query": {
            "type": "string",
            "description": (
                "The search query. Be specific: include relevant keywords, dates, or context. "
                "Examples: 'current EUR/CZK exchange rate', 'Prague weather today', "
                "'Python 3.13 new features'."
            ),
        },
        "max_results": {
            "type": "integer",
            "description": "Number of results to return (default: 5, max: 10). Use 3 for quick facts.",
        },
    },
    "required": ["query"],
}


async def web_search(query: str, max_results: int = 5) -> str:
    """
    Search the web and return a concise summary of the top results.
    Use this whenever the user asks about current events, news, weather,
    prices, sports scores, or any fact that may have changed after your
    training cut-off. Always search rather than guessing or refusing.
    Returns a direct answer (when available) followed by titled snippets with URLs.
    """
    api_key = os.getenv("TAVILY_API_KEY")
    if not api_key:
        return (
            "Webové vyhledávání není dostupné: TAVILY_API_KEY není nastaven v .env. "
            "Klíč zdarma získáš na: https://app.tavily.com/"
        )

    max_results = max(1, min(max_results, 10))

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            response = await client.post(
                _TAVILY_URL,
                json={
                    "api_key": api_key,
                    "query": query,
                    "max_results": max_results,
                    "include_answer": True,
                },
            )
            response.raise_for_status()
            data = response.json()
    except httpx.TimeoutException:
        logger.warning("web_search timed out: %r", query)
        return f"web_search timed out for: {query!r}"
    except httpx.HTTPStatusError as exc:
        logger.error("web_search HTTP %s for: %r", exc.response.status_code, query)
        return f"web_search failed (HTTP {exc.response.status_code}) for: {query!r}"
    except Exception as exc:
        logger.exception("web_search error for: %r", query)
        return f"web_search failed: {exc}"

    lines = [f"Search: {query}\n"]

    answer = data.get("answer", "").strip()
    if answer:
        lines.append(f"Answer: {answer}\n")

    for i, r in enumerate(data.get("results", []), start=1):
        content = r.get("content", "").strip()
        if len(content) > 500:
            content = content[:497] + "..."
        lines.append(f"[{i}] {r.get('title', 'Untitled')}\n{r.get('url', '')}\n{content}\n")

    if not data.get("results") and not answer:
        lines.append("No results found.")

    return "\n".join(lines)
