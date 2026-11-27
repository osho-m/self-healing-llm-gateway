"""Provider registry."""

from __future__ import annotations

from app.http import HttpClient
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
from app.providers.local import LocalModelProvider
from app.providers.mock import MockProvider
from app.providers.openai import OllamaProvider, OpenAICompatibleProvider

_KINDS: dict[str, type[Provider]] = {
    "mock": MockProvider,
    "openai": OpenAICompatibleProvider,
    "ollama": OllamaProvider,
    "anthropic": AnthropicProvider,
    "local": LocalModelProvider,
}


def build_provider(config: ProviderConfig, client: HttpClient) -> Provider:
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
