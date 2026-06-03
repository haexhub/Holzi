import httpx

from hermes.signal.client import SignalClient


def test_ws_url_translates_http_to_ws() -> None:
    http = httpx.AsyncClient(base_url="http://signal-cli-rest-api:8080")
    client = SignalClient(http, "+491701234567")

    assert client._ws_url() == "ws://signal-cli-rest-api:8080/v1/receive/+491701234567"


def test_ws_url_translates_https_to_wss() -> None:
    http = httpx.AsyncClient(base_url="https://signal.example.com")
    client = SignalClient(http, "+491701234567")

    assert client._ws_url() == "wss://signal.example.com/v1/receive/+491701234567"
