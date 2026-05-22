import asyncio
import json
from typing import Any

import httpx
import trafilatura

from hermes.agent import Tool

BRAVE_SEARCH_URL = "https://api.search.brave.com/res/v1/web/search"


def build_external_tools(
    http: httpx.AsyncClient | None,
    brave_api_key: str | None,
) -> list[Tool]:
    return [
        _web_search(http, brave_api_key),
        _url_fetch(http),
    ]


# ---------------------------------------------------------------------------
def _web_search(http: httpx.AsyncClient | None, api_key: str | None) -> Tool:
    async def handler(args: dict[str, Any]) -> str:
        if http is None or not api_key:
            return json.dumps(
                {"error": "web_search requires HERMES_BRAVE_API_KEY to be set"}
            )
        query = str(args.get("query", "")).strip()
        if not query:
            return json.dumps({"error": "query must be a non-empty string"})
        n = max(1, min(int(args.get("n", 5)), 20))
        try:
            resp = await http.get(
                BRAVE_SEARCH_URL,
                params={"q": query, "count": n},
                headers={
                    "X-Subscription-Token": api_key,
                    "Accept": "application/json",
                },
                timeout=15.0,
            )
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            return json.dumps({"error": f"brave search failed: {exc}"})

        data = resp.json()
        results = [
            {
                "title": item.get("title", ""),
                "url": item.get("url", ""),
                "description": item.get("description", ""),
            }
            for item in (data.get("web") or {}).get("results", [])[:n]
        ]
        return json.dumps(results)

    return Tool(
        name="web_search",
        description=(
            "Search the open web via Brave Search. Returns up to `n` "
            "{title, url, description} entries. Requires HERMES_BRAVE_API_KEY."
        ),
        parameters_schema={
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "n": {"type": "integer", "default": 5, "minimum": 1, "maximum": 20},
            },
            "required": ["query"],
        },
        handler=handler,
    )


# ---------------------------------------------------------------------------
def _url_fetch(http: httpx.AsyncClient | None) -> Tool:
    async def handler(args: dict[str, Any]) -> str:
        if http is None:
            return json.dumps({"error": "url_fetch needs an http client"})
        url = str(args.get("url", "")).strip()
        if not url:
            return json.dumps({"error": "url must be a non-empty string"})
        try:
            resp = await http.get(url, follow_redirects=True, timeout=20.0)
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            return json.dumps({"error": f"fetch failed: {exc}"})

        html = resp.text
        # trafilatura is sync — run it off the event loop.
        text = await asyncio.to_thread(
            trafilatura.extract,
            html,
            include_comments=False,
            include_tables=False,
        )
        return json.dumps({"url": url, "text": text or ""})

    return Tool(
        name="url_fetch",
        description=(
            "Fetch the given URL and return its main extracted text content "
            "(via trafilatura, comments and tables stripped)."
        ),
        parameters_schema={
            "type": "object",
            "properties": {"url": {"type": "string"}},
            "required": ["url"],
        },
        handler=handler,
    )
