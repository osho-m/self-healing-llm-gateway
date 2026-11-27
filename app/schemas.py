"""Public request/response contract (OpenAI-compatible chat completions)."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from app.policy import Priority


class ChatMessage(BaseModel):
    role: Literal["system", "user", "assistant", "tool"]
    content: str = Field(max_length=200_000)


class ChatRequest(BaseModel):
    model: str = Field(description="Capability tier, e.g. economy | balanced | premium")
    messages: list[ChatMessage] = Field(min_length=1, max_length=256)
    max_tokens: int = Field(default=512, ge=1, le=32_000)
    temperature: float | None = Field(default=None, ge=0, le=2)
    stream: bool = False
    priority: Priority = "realtime"
    user: str | None = Field(default=None, max_length=256)


class Usage(BaseModel):
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int


class Choice(BaseModel):
    index: int = 0
    message: ChatMessage
    finish_reason: str = "stop"


class GatewayMeta(BaseModel):
    request_id: str
    tier: str
    provider: str
    served_model: str
    fallback_used: bool
    attempts: list[str]
    cost_usd: float
    latency_ms: float
    budget_utilization: float


class ChatResponse(BaseModel):
    id: str
    object: str = "chat.completion"
    created: int
    model: str
    choices: list[Choice]
    usage: Usage
    x_gateway: GatewayMeta


class TeamPatch(BaseModel):
    requests_per_minute: int | None = Field(default=None, ge=1)
    tokens_per_minute: int | None = Field(default=None, ge=1)
    daily_budget_usd: float | None = Field(default=None, ge=0)
    budget_warning_ratio: float | None = Field(default=None, gt=0, le=1)
    batch_reserve_ratio: float | None = Field(default=None, ge=0, lt=1)
    allowed_tiers: list[str] | None = None
