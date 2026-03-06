import httpx

from app.llm.base import LLMProvider, LLMResponse


class OpenRouterProvider(LLMProvider):
    name = "openrouter"

    def __init__(self, api_key: str, model: str = "anthropic/claude-opus-4.6"):
        self.api_key = api_key
        self.model = model
        self.base_url = "https://openrouter.ai/api/v1"

    async def generate(
        self, prompt: str, system: str, temperature: float = 0.3,
        images: list[dict] | None = None,
    ) -> LLMResponse:
        # Build user content — text or multimodal
        if images:
            user_content: list[dict] = []
            for img in images:
                user_content.append({
                    "type": "image_url",
                    "image_url": {
                        "url": f"data:{img['media_type']};base64,{img['data']}",
                    },
                })
            user_content.append({"type": "text", "text": prompt})
        else:
            user_content = prompt  # type: ignore

        async with httpx.AsyncClient(timeout=120.0) as client:
            resp = await client.post(
                f"{self.base_url}/chat/completions",
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": self.model,
                    "messages": [
                        {"role": "system", "content": system},
                        {"role": "user", "content": user_content},
                    ],
                    "temperature": temperature,
                },
            )
            resp.raise_for_status()
            data = resp.json()

        choice = data["choices"][0]["message"]["content"]
        usage = data.get("usage", {})
        tokens = usage.get("total_tokens", 0)

        return LLMResponse(text=choice, tokens_used=tokens, provider=self.name)

    async def health_check(self) -> bool:
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(
                    f"{self.base_url}/models",
                    headers={"Authorization": f"Bearer {self.api_key}"},
                )
                return resp.status_code == 200
        except Exception:
            return False
