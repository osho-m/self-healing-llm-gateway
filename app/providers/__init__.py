"""Provider registry."""

from __future__ import annotations

import httpx

from app.policy import ProviderConfig
from app.providers.anthropic import AnthropicProvider
from app.providers.base import (
    CompletionRequest,
    CompletionResult,
    Provider,
    ProviderError,
    StreamChunk,
    estimate_tokens,
)
from app.providers.mock import MockProvider
from app.providers.openai import OllamaProvider, OpenAICompatibleProvider

_KINDS: dict[str, type[Provider]] = {
    "mock": MockProvider,
    "openai": OpenAICompatibleProvider,
    "ollama": OllamaProvider,
    "anthropic": AnthropicProvider,
}


def build_provider(config: ProviderConfig, client: httpx.AsyncClient) -> Provider:
    return _KINDS[config.kind](config, client)


__all__ = [
    "CompletionRequest",
    "CompletionResult",
    "Provider",
    "ProviderError",
    "StreamChunk",
    "build_provider",
    "estimate_tokens",
]
