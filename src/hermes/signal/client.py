import json
from collections.abc import AsyncIterator
from typing import Any

import httpx
import websockets


class SignalClient:
    def __init__(self, http: httpx.AsyncClient, number: str) -> None:
        self.http = http
        self.number = number

    def _ws_url(self) -> str:
        base = str(self.http.base_url).rstrip("/")
        if base.startswith("https://"):
            ws_base = "wss://" + base[len("https://") :]
        elif base.startswith("http://"):
            ws_base = "ws://" + base[len("http://") :]
        else:
            ws_base = base
        return f"{ws_base}/v1/receive/{self.number}"

    async def receive_stream(self) -> AsyncIterator[dict[str, Any]]:
        """Open a WebSocket to signal-cli-rest-api's /v1/receive endpoint
        (json-rpc mode contract) and yield each decoded envelope dict.

        The HTTP GET variant is intentionally not used — in json-rpc mode it
        silently no-ops. Reconnect policy lives in the caller (SignalWorker).
        """
        url = self._ws_url()
        async with websockets.connect(url, ping_interval=20, ping_timeout=10) as ws:
            async for raw in ws:
                payload_text = raw.decode("utf-8") if isinstance(raw, bytes) else raw
                payload = json.loads(payload_text)
                if isinstance(payload, dict):
                    yield payload

    async def send(self, *, recipient: str, message: str) -> None:
        response = await self.http.post(
            "/v2/send",
            json={
                "message": message,
                "number": self.number,
                "recipients": [recipient],
            },
        )
        response.raise_for_status()


# Module-level helpers for the link-as-secondary-device flow. They don't
# need a bound number — that's the whole point, signal-cli discovers it
# from the primary device after the QR is scanned.


async def start_qr_link(http: httpx.AsyncClient, *, device_name: str) -> bytes:
    """Ask signal-cli-rest-api to generate a linking QR. Returns the
    PNG bytes — caller forwards them as `image/png` to the browser.

    Endpoint: GET /v1/qrcodelink?device_name=…. signal-cli holds the
    request open while it waits for the primary device to scan the QR
    and confirm. Default timeout in signal-cli-rest-api is generous
    (~120s); we set a matching timeout on the call so a slow scan
    doesn't trip our default 30s.
    """
    response = await http.get(
        "/v1/qrcodelink",
        params={"device_name": device_name},
        timeout=180.0,
    )
    response.raise_for_status()
    return response.content


async def list_registered_numbers(http: httpx.AsyncClient) -> list[str]:
    """Return the E.164 numbers signal-cli currently knows about.

    The link flow uses this to spot a freshly-linked number: snapshot
    the list before `start_qr_link`, snapshot again after the QR is
    scanned, the new entry is the linked number.

    Endpoint: GET /v1/accounts → ["+49123…", …] (just a list of strings).
    """
    response = await http.get("/v1/accounts", timeout=15.0)
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, list):
        raise ValueError(
            f"signal-cli /v1/accounts returned non-list payload: {type(payload).__name__}"
        )
    return [str(n) for n in payload]
