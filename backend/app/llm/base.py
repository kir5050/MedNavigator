from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass
class LLMResponse:
    text: str
    tokens_used: int
    provider: str
    cached: bool = False


class LLMProvider(ABC):
    name: str

    @abstractmethod
    async def generate(
        self, prompt: str, system: str, temperature: float = 0.3,
        images: list[dict] | None = None,
    ) -> LLMResponse:
        ...

    @abstractmethod
    async def health_check(self) -> bool:
        ...
