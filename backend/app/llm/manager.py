import hashlib
import logging

from diskcache import Cache

from app.llm.base import LLMProvider, LLMResponse

logger = logging.getLogger(__name__)


class LLMManager:
    def __init__(self, providers: list[LLMProvider], cache_dir: str = "./cache"):
        self.providers = providers
        self.cache = Cache(cache_dir)

    def _cache_key(self, prompt: str, system: str) -> str:
        raw = f"{system}||{prompt}"
        return hashlib.sha256(raw.encode()).hexdigest()

    async def generate(
        self,
        prompt: str,
        system: str,
        temperature: float = 0.3,
        use_cache: bool = True,
        cache_ttl: int = 86400,
        images: list[dict] | None = None,
    ) -> LLMResponse:
        # Don't cache image requests
        if images:
            use_cache = False

        if use_cache:
            key = self._cache_key(prompt, system)
            cached = self.cache.get(key)
            if cached is not None:
                logger.info("Cache hit for prompt")
                return LLMResponse(
                    text=cached, tokens_used=0, provider="cache", cached=True
                )

        last_error: Exception | None = None
        for provider in self.providers:
            try:
                logger.info("Trying provider: %s", provider.name)
                response = await provider.generate(prompt, system, temperature, images=images)
                if use_cache:
                    self.cache.set(key, response.text, expire=cache_ttl)
                return response
            except Exception as e:
                logger.warning("Provider %s failed: %s", provider.name, e)
                last_error = e
                continue

        raise RuntimeError(f"All LLM providers failed. Last error: {last_error}")

    async def close(self):
        self.cache.close()
