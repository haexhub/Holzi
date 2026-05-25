from typing import Any

import httpx


class SignalClient:
    def __init__(self, http: httpx.AsyncClient, number: str) -> None:
        self.http = http
        self.number = number

    async def receive(self, *, timeout: int = 30) -> list[dict[str, Any]]:
        response = await self.http.get(
            f"/v1/receive/{self.number}",
            params={"timeout": timeout},
            timeout=timeout + 5,
        )
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, list):
            raise ValueError(
                f"signal-cli /v1/receive returned non-list payload: {type(payload).__name__}"
            )
        return payload

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

    Endpoint: POST /v1/qrcodelink/{device_name}. signal-cli holds the
    request open while it waits for the primary device to scan the QR
    and confirm. Default timeout in signal-cli-rest-api is generous
    (~120s); we set a matching timeout on the call so a slow scan
    doesn't trip our default 30s.
    """
    response = await http.post(
        f"/v1/qrcodelink/{device_name}",
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
