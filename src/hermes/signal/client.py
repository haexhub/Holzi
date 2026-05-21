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
        return list(response.json())

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
