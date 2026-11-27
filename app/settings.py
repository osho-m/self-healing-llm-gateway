"""Process-level settings, read from environment variables (prefix ``GATEWAY_``)."""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="GATEWAY_", env_file=".env", extra="ignore")

    environment: Literal["development", "test", "production"] = "development"
    policy_path: str = "config/policy.example.yml"
    policy_reload_interval_s: float = Field(default=5.0, ge=0.0)

    # Separate admin credential. Required in production.
    admin_api_key: str | None = None

    # Storage. Empty -> in-process stores (single replica only).
    redis_url: str | None = None
    redis_max_connections: int = Field(default=200, ge=1)

    # Observability
    log_level: str = "INFO"
    log_json: bool = True
    otel_exporter_otlp_endpoint: str | None = None
    otel_service_name: str = "llm-gateway"
    otel_sample_ratio: float = Field(default=1.0, ge=0.0, le=1.0)

    audit_log_path: str | None = "data/audit.log"
    health_checks_enabled: bool = True

    @model_validator(mode="after")
    def _production_guards(self) -> Settings:
        if self.environment == "production":
            if not self.admin_api_key or len(self.admin_api_key) < 24:
                raise ValueError("GATEWAY_ADMIN_API_KEY (>=24 chars) is required in production")
            if not self.redis_url:
                raise ValueError("GATEWAY_REDIS_URL is required in production (in-memory stores are per-process)")
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
