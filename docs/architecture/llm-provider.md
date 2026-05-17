# LLM Provider Abstraction

## Provider Interface

```python
from abc import ABC, abstractmethod
from dataclasses import dataclass

@dataclass
class LLMResponse:
    text: str
    tokens_used: int
    cached: bool = False

class LLMProvider(ABC):
    @abstractmethod
    async def generate(self, prompt: str, system: str, temperature: float = 0.3) -> LLMResponse:
        """Send prompt to LLM and return response."""
        ...

    @abstractmethod
    async def health_check(self) -> bool:
        """Check if provider is available."""
        ...
```

## Providers

### OpenRouter (current implementation)
- API: https://openrouter.ai/api/v1/chat/completions
- Auth: API key (Bearer token)
- OpenAI-compatible API format
- Default model: `anthropic/claude-opus-4.6` (overridable via `OPENROUTER_MODEL`)
- Access to multiple models behind a single API (Claude, GPT, Llama, etc.)

### Provider Manager
```python
class LLMManager:
    def __init__(self, providers: list[LLMProvider], cache: Cache):
        self.providers = providers  # ordered by priority
        self.cache = cache

    async def generate(self, prompt: str, system: str, use_cache: bool = True) -> LLMResponse:
        # 1. Check cache
        if use_cache:
            cached = self.cache.get(prompt, system)
            if cached:
                return LLMResponse(text=cached, tokens_used=0, cached=True)

        # 2. Try providers in order (first available wins)
        response = None
        for provider in self.providers:
            try:
                response = await provider.generate(prompt, system)
                break
            except Exception:
                continue
        if response is None:
            raise RuntimeError("All LLM providers failed")

        # 4. Cache result
        if use_cache:
            self.cache.set(prompt, system, response.text)

        return response
```

## Prompt Chain

Session goes through these stages sequentially:

### Stage 1: Symptom Extraction
- System prompt instructs LLM to extract structured symptoms from free text
- Output: list of symptoms with body location, duration, intensity

### Stage 2: Clarification
- Based on extracted symptoms, generate targeted follow-up questions
- Maximum 3-5 clarifying questions per session
- Check for red flags at each step

### Stage 3: Triage Assessment
- With full symptom picture, assess urgency level
- Map symptoms to potential medical areas (NOT diagnoses)
- Determine specialist routing

### Stage 4: Routing
- Match symptom areas to medical specialties
- Use Medical KB for specialty definitions
- Generate 1-3 specialist recommendations with priorities

### Stage 5: Visit Preparation
- Generate preparation tips for the recommended specialist
- What tests might be useful to bring
- Questions to ask the doctor
- Summary for PDF generation

## Red Flag Detection
Checked at EVERY stage, before LLM call:

```python
RED_FLAGS = [
    "боль в груди", "давит в груди",
    "потеря сознания", "обморок",
    "сильное кровотечение",
    "не могу дышать", "задыхаюсь", "сильная одышка",
    "онемение половины тела", "не двигается рука/нога",
    "перекосило лицо", "не могу говорить",
    "судороги",
    "острая боль в животе",
    "суицид", "не хочу жить",
]
```

If detected -> immediately return emergency response with 103/112 recommendation.

## Caching Strategy

- **Cache key:** hash(normalized_symptoms + prompt_template_version)
- **TTL:** 24 hours for clarification prompts, 7 days for triage results
- **Invalidation:** on prompt template update (version in key)
- **Storage:** diskcache (file-based, persistent across restarts)
- **Expected hit rate:** ~30-40% for common symptom combinations
