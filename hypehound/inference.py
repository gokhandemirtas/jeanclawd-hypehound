from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.openai import OpenAIProvider

from .config import settings


def get_model() -> OpenAIChatModel:
    """
    Returns a PydanticAI model pointed at the configured OpenAI-compatible endpoint.
    Works with LM Studio, Ollama, vLLM, or any remote OpenAI-compatible provider —
    change INFERENCE_BASE_URL and INFERENCE_MODEL in .env to swap.
    """
    return OpenAIChatModel(
        settings.inference_model,
        provider=OpenAIProvider(base_url=settings.inference_base_url),
    )
