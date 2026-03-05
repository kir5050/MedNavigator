import httpx

from app.llm.base import LLMProvider, LLMResponse


class GigaChatProvider(LLMProvider):
    name = "gigachat"

    def __init__(self, client_id: str, client_secret: str):
        self.client_id = client_id
        self.client_secret = client_secret
        self._access_token: str | None = None

    async def _get_token(self) -> str:
        if self._access_token:
            return self._access_token

        async with httpx.AsyncClient(timeout=15.0, verify=False) as client:
            resp = await client.post(
                "https://ngw.devices.sberbank.ru:9443/api/v2/oauth",
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                data={
                    "scope": "GIGACHAT_API_PERS",
                    "grant_type": "client_credentials",
                },
                auth=(self.client_id, self.client_secret),
            )
            resp.raise_for_status()
            self._access_token = resp.json()["access_token"]

        return self._access_token

    async def generate(
        self, prompt: str, system: str, temperature: float = 0.3
    ) -> LLMResponse:
        token = await self._get_token()

        async with httpx.AsyncClient(timeout=60.0, verify=False) as client:
            resp = await client.post(
                "https://gigachat.devices.sberbank.ru/api/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": "GigaChat",
                    "messages": [
                        {"role": "system", "content": system},
                        {"role": "user", "content": prompt},
                    ],
                    "temperature": temperature,
                },
            )
            if resp.status_code == 401:
                self._access_token = None
                token = await self._get_token()
                resp = await client.post(
                    "https://gigachat.devices.sberbank.ru/api/v1/chat/completions",
                    headers={
                        "Authorization": f"Bearer {token}",
                        "Content-Type": "application/json",
                    },
                    json={
                        "model": "GigaChat",
                        "messages": [
                            {"role": "system", "content": system},
                            {"role": "user", "content": prompt},
                        ],
                        "temperature": temperature,
                    },
                )
            resp.raise_for_status()
            data = resp.json()

        text = data["choices"][0]["message"]["content"]
        tokens = data.get("usage", {}).get("total_tokens", 0)

        return LLMResponse(text=text, tokens_used=tokens, provider=self.name)

    async def health_check(self) -> bool:
        try:
            await self._get_token()
            return True
        except Exception:
            return False
