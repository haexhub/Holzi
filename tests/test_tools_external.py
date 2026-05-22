import json

import httpx

from hermes.tools.external import build_external_tools


def _by_name(tools, name):
    for t in tools:
        if t.name == name:
            return t
    raise AssertionError(f"tool not found: {name}")


def _make_http(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="http://placeholder",
    )


# ---------------------------------------------------------------------------
# web_search
# ---------------------------------------------------------------------------
async def test_web_search_without_api_key_returns_error() -> None:
    tools = build_external_tools(_make_http(lambda r: httpx.Response(200)), None)
    tool = _by_name(tools, "web_search")
    data = json.loads(await tool.handler({"query": "anything"}))
    assert "error" in data


async def test_web_search_returns_results() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200,
            json={
                "web": {
                    "results": [
                        {
                            "title": "Result A",
                            "url": "https://example.com/a",
                            "description": "first hit",
                        },
                        {
                            "title": "Result B",
                            "url": "https://example.com/b",
                            "description": "second hit",
                        },
                    ]
                }
            },
        )

    tools = build_external_tools(_make_http(handler), "fake-brave-key")
    tool = _by_name(tools, "web_search")
    data = json.loads(await tool.handler({"query": "python", "n": 2}))
    assert [r["url"] for r in data] == ["https://example.com/a", "https://example.com/b"]

    # API key sent as expected.
    assert seen[0].headers["X-Subscription-Token"] == "fake-brave-key"
    assert seen[0].url.params["q"] == "python"
    assert seen[0].url.params["count"] == "2"


async def test_web_search_handles_upstream_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": "down"})

    tools = build_external_tools(_make_http(handler), "fake-brave-key")
    tool = _by_name(tools, "web_search")
    data = json.loads(await tool.handler({"query": "test"}))
    assert "error" in data


async def test_web_search_rejects_empty_query() -> None:
    tools = build_external_tools(_make_http(lambda r: httpx.Response(200)), "k")
    tool = _by_name(tools, "web_search")
    data = json.loads(await tool.handler({"query": "   "}))
    assert "error" in data


# ---------------------------------------------------------------------------
# url_fetch
# ---------------------------------------------------------------------------
async def test_url_fetch_extracts_main_content() -> None:
    html = b"""<!DOCTYPE html>
<html><body>
<nav>menu</nav>
<article>
  <h1>The Title</h1>
  <p>This is the main body of the article. It has multiple sentences so
     trafilatura is confident that this is the primary content.
     Important keyword: zorblax.</p>
</article>
<footer>copyright</footer>
</body></html>"""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=html, headers={"content-type": "text/html"})

    tools = build_external_tools(_make_http(handler), None)
    tool = _by_name(tools, "url_fetch")
    data = json.loads(await tool.handler({"url": "http://example.com/article"}))
    assert "zorblax" in data["text"]
    assert "menu" not in data["text"]


async def test_url_fetch_returns_error_for_http_failure() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, content=b"not found")

    tools = build_external_tools(_make_http(handler), None)
    tool = _by_name(tools, "url_fetch")
    data = json.loads(await tool.handler({"url": "http://example.com/missing"}))
    assert "error" in data


async def test_url_fetch_rejects_empty_url() -> None:
    tools = build_external_tools(_make_http(lambda r: httpx.Response(200)), None)
    tool = _by_name(tools, "url_fetch")
    data = json.loads(await tool.handler({"url": ""}))
    assert "error" in data


async def test_url_fetch_rejects_unsafe_targets() -> None:
    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
        raise AssertionError(f"network call must not happen: {request.url}")

    tools = build_external_tools(_make_http(handler), None)
    tool = _by_name(tools, "url_fetch")

    for bad in [
        "file:///etc/passwd",
        "ftp://example.com/x",
        "http://localhost/",
        "http://127.0.0.1/admin",
        "http://10.0.0.1/internal",
        "http://192.168.1.1/router",
        "http://169.254.169.254/latest/meta-data/",
        "http://[::1]/x",
    ]:
        data = json.loads(await tool.handler({"url": bad}))
        assert "error" in data, f"expected reject for {bad}"
