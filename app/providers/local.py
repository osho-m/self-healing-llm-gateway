"""In-process model provider (``kind: local``).

Runs a model inside the gateway process, e.g. a Hugging Face Transformers model on a GPU. The
policy names the model with ``model:``; the hosting code registers a generate function for that
name with :func:`register_local_model`. Keeping the model runtime outside this package means the
gateway does not depend on torch/transformers, and platform-specific wrappers (such as Hugging Face
ZeroGPU's ``@spaces.GPU``) stay in the deployment code.

A provider whose model is not registered reports itself unavailable, so the gateway skips it the
same way it skips a provider whose API key is not set.

A timeout cannot stop a running thread: Python has no way to cancel it. So after a timeout the
model stays unavailable until that worker actually finishes. The gateway then fails over to the
next provider instead of starting a second generation on the same model.
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass

from app.providers.base import CompletionRequest, CompletionResult, Provider, ProviderError, StreamChunk


@dataclass(frozen=True)
class LocalGeneration:
    text: str
    prompt_tokens: int
    completion_tokens: int


# (messages, max_tokens) -> LocalGeneration. Called in a worker thread; may block.
GenerateFn = Callable[[list[dict[str, str]], int], LocalGeneration]

_REGISTRY: dict[str, GenerateFn] = {}
# model -> "worker finished" events, one per generation that timed out while running.
_STALLED: dict[str, list[threading.Event]] = {}
_LOCK = threading.Lock()


def _stalled(model: str) -> bool:
    with _LOCK:
        pending = [done for done in _STALLED.get(model, []) if not done.is_set()]
        if pending:
            _STALLED[model] = pending
            return True
        _STALLED.pop(model, None)
        return False


def register_local_model(model: str, generate: GenerateFn) -> None:
    with _LOCK:
        _REGISTRY[model] = generate


def unregister_local_model(model: str) -> None:
    with _LOCK:
        _REGISTRY.pop(model, None)


class LocalModelProvider(Provider):
    def is_available(self) -> bool:
        return self.config.model in _REGISTRY and not _stalled(self.config.model)

    async def complete(self, request: CompletionRequest) -> CompletionResult:
        generate = _REGISTRY.get(self.config.model)
        if generate is None:
            raise ProviderError(self.name, "not_registered", f"no local model {self.config.model!r}", retryable=False)
        model = self.config.model
        if _stalled(model):
            raise ProviderError(self.name, "busy", "an earlier generation is still running", retryable=True)
        started, done = threading.Event(), threading.Event()

        def run() -> LocalGeneration:
            started.set()
            try:
                return generate(request.messages, request.max_tokens)
            finally:
                done.set()

        try:
            # asyncio.to_thread copies contextvars, so request-scoped context reaches the runtime.
            out = await asyncio.wait_for(asyncio.to_thread(run), timeout=self.config.timeout_s)
        except TimeoutError as exc:
            with _LOCK:
                # A call cancelled while still queued never runs, so it must not block the model.
                if started.is_set() and not done.is_set():
                    _STALLED.setdefault(model, []).append(done)  # unavailable until every such worker exits
            raise ProviderError(self.name, "timeout", "local generation timed out", retryable=True) from exc
        except Exception as exc:
            # Runtime failures (GPU quota exhausted, out of memory, device errors) are transient from
            # the gateway's point of view: retry, then fail over to the next provider in the tier.
            raise ProviderError(self.name, "local_runtime", f"{type(exc).__name__}: {exc}", retryable=True) from exc
        return CompletionResult(
            content=out.text,
            prompt_tokens=out.prompt_tokens,
            completion_tokens=out.completion_tokens,
            model=self.config.model,
        )

    async def stream(self, request: CompletionRequest) -> AsyncIterator[StreamChunk]:
        # Generation runs to completion in one call; relay it word by word so clients see SSE.
        result = await self.complete(request)
        for i, word in enumerate(result.content.split(" ")):
            yield StreamChunk(delta=word if i == 0 else " " + word)
        yield StreamChunk(
            prompt_tokens=result.prompt_tokens, completion_tokens=result.completion_tokens, finish_reason="stop"
        )
