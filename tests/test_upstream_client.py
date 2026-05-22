from hermes.main import build_upstream_client


def test_upstream_client_without_api_key_has_no_auth_header() -> None:
    client = build_upstream_client("http://haex-claude-proxy:8080", "")
    assert "authorization" not in {k.lower() for k in client.headers}


def test_upstream_client_with_api_key_adds_bearer_header() -> None:
    client = build_upstream_client("https://api.openai.com", "sk-test-abc")
    assert client.headers["Authorization"] == "Bearer sk-test-abc"


def test_upstream_client_uses_provided_base_url() -> None:
    client = build_upstream_client("https://openrouter.ai/api/v1", "")
    assert str(client.base_url).rstrip("/") == "https://openrouter.ai/api/v1"
