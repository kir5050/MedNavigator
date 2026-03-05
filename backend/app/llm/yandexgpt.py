import httpx

from app.llm.base import LLMProvider, LLMResponse


class YandexGPTProvider(LLMProvider):
    name = "yandexgpt"

    def __init__(self, api_key: str, folder_id: str, model: str = "yandexgpt-lite"):
        self.api_key = api_key
        self.folder_id = folder_id
        self.model_uri = f"gpt://{folder_id}/{model}"

    async def generate(
        self, prompt: str, system: str, temperature: float = 0.3
    ) -> LLMResponse:
        async with httpx.AsyncClient(timeout=60.0) as client:
            resp = await client.post(
                "https://llm.api.cloud.yandex.net/foundationModels/v1/completion",
                headers={
                    "Authorization": f"Api-Key {self.api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "modelUri": self.model_uri,
                    "completionOptions": {
                        "stream": False,
                        "temperature": temperature,
                    },
                    "messages": [
                        {"role": "system", "text": system},
                        {"role": "user", "text": prompt},
                    ],
                },
            )
            resp.raise_for_status()
            data = resp.json()

        result = data["result"]
        text = result["alternatives"][0]["message"]["text"]
        tokens = int(result["usage"]["totalTokens"])

        return LLMResponse(text=text, tokens_used=tokens, provider=self.name)

    async def health_check(self) -> bool:
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.post(
                    "https://llm.api.cloud.yandex.net/foundationModels/v1/completion",
                    headers={
                        "Authorization": f"Api-Key {self.api_key}",
                        "Content-Type": "application/json",
                    },
                    json={
                        "modelUri": self.model_uri,
                        "completionOptions": {"stream": False, "temperature": 0.1},
                        "messages": [{"role": "user", "text": "ping"}],
                    },
                )
                return resp.status_code == 200
        except Exception:
            return False
