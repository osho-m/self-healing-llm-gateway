"""Declarative gateway policy (YAML) with hot reload and runtime overrides.

Precedence (highest first):
  1. Runtime overrides set through ``PATCH /v1/admin/teams/{id}`` (shared via Redis when configured).
  2. The YAML policy file, re-read whenever its mtime changes.

Overrides survive hot reloads. Clear them with ``DELETE /v1/admin/teams/{id}/overrides``
once the YAML file has been updated, so the file stays the long-term source of truth.
"""

from __future__ import annotations

import hashlib
import logging
import os
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field, field_validator, model_validator

log = logging.getLogger(__name__)

Priority = Literal["realtime", "batch"]


def hash_key(api_key: str) -> str:
    return hashlib.sha256(api_key.encode()).hexdigest()


class Enrichment(BaseModel):
    system_prompt: str | None = None
    disclaimer: str | None = None
    blocked_terms: list[str] = Field(default_factory=list)


class TeamPolicy(BaseModel):
    team_id: str
    name: str = ""
    # Store only hashes. ``api_key`` (plaintext) is accepted for local demos and hashed on load.
    api_key_sha256: list[str] = Field(default_factory=list)
    role: Literal["team", "admin"] = "team"
    allowed_tiers: list[str] = Field(default_factory=lambda: ["economy", "balanced"])
    requests_per_minute: int = Field(default=60, ge=1)
    tokens_per_minute: int = Field(default=60_000, ge=1)
    daily_budget_usd: float = Field(default=10.0, ge=0)
    budget_warning_ratio: float = Field(default=0.8, gt=0, le=1)
    # Fraction of each bucket that batch traffic may not consume, kept for realtime traffic.
    batch_reserve_ratio: float = Field(default=0.2, ge=0, lt=1)
    enrichment: Enrichment = Field(default_factory=Enrichment)

    @model_validator(mode="before")
    @classmethod
    def _hash_plaintext_keys(cls, data: Any) -> Any:
        if isinstance(data, dict) and data.get("api_keys"):
            data = dict(data)
            hashes = list(data.get("api_key_sha256") or [])
            hashes += [hash_key(k) for k in data.pop("api_keys")]
            data["api_key_sha256"] = hashes
        return data


class ProviderConfig(BaseModel):
    name: str
    kind: Literal["mock", "openai", "anthropic", "ollama"]
    model: str = "mock-model"
    base_url: str | None = None
    api_key_env: str | None = None
    timeout_s: float = Field(default=30.0, gt=0)
    # USD per 1M tokens
    input_price_per_mtok: float = Field(default=0.0, ge=0)
    output_price_per_mtok: float = Field(default=0.0, ge=0)
    # Mock-only tuning
    mock_latency_ms: float = 0.0
    # Anthropic only: reasoning effort, and a floor for max_tokens. Current Claude models always
    # think and thinking tokens count against max_tokens, so tiny caps can leave no room for the answer.
    effort: Literal["low", "medium", "high", "xhigh", "max"] | None = None
    min_max_tokens: int = Field(default=0, ge=0)
    # Background health probes send a real request every interval. Turn them off for paid providers;
    # an open circuit then recovers through the single half-open live request after the cooldown.
    probe: bool = True

    def api_key(self) -> str | None:
        return os.environ.get(self.api_key_env) if self.api_key_env else None

    def is_configured(self) -> bool:
        """False when the provider needs an API key that is not set (it is then skipped, not called)."""
        return self.api_key_env is None or bool(self.api_key())

    def cost_usd(self, prompt_tokens: int, completion_tokens: int) -> float:
        return (prompt_tokens * self.input_price_per_mtok + completion_tokens * self.output_price_per_mtok) / 1e6


class ResiliencePolicy(BaseModel):
    max_retries: int = Field(default=3, ge=0, le=10)
    backoff_base_s: float = Field(default=0.1, ge=0)
    backoff_max_s: float = Field(default=2.0, ge=0)
    circuit_failure_threshold: int = Field(default=3, ge=1)
    circuit_window_s: float = Field(default=30.0, gt=0)
    circuit_cooldown_s: float = Field(default=15.0, gt=0)
    health_check_interval_s: float = Field(default=30.0, gt=0)
    health_window_s: float = Field(default=300.0, gt=0)
    degraded_error_rate: float = Field(default=0.05, ge=0, le=1)
    down_error_rate: float = Field(default=0.5, ge=0, le=1)
    degraded_p99_ms: float = Field(default=5000.0, gt=0)


class Policy(BaseModel):
    providers: list[ProviderConfig]
    tiers: dict[str, list[str]]
    teams: list[TeamPolicy]
    resilience: ResiliencePolicy = Field(default_factory=ResiliencePolicy)

    @field_validator("tiers")
    @classmethod
    def _non_empty_chains(cls, v: dict[str, list[str]]) -> dict[str, list[str]]:
        for tier, chain in v.items():
            if not chain:
                raise ValueError(f"tier {tier!r} has an empty fallback chain")
        return v

    @model_validator(mode="after")
    def _references_resolve(self) -> Policy:
        names = {p.name for p in self.providers}
        if len(names) != len(self.providers):
            raise ValueError("provider names must be unique")
        for tier, chain in self.tiers.items():
            missing = [p for p in chain if p not in names]
            if missing:
                raise ValueError(f"tier {tier!r} references unknown providers {missing}")
        team_ids = [t.team_id for t in self.teams]
        if len(set(team_ids)) != len(team_ids):
            raise ValueError("team_id values must be unique")
        seen: set[str] = set()
        for team in self.teams:
            unknown = set(team.allowed_tiers) - set(self.tiers)
            if unknown:
                raise ValueError(f"team {team.team_id!r} allows unknown tiers {sorted(unknown)}")
            dup = seen.intersection(team.api_key_sha256)
            if dup:
                raise ValueError("API key hashes must be unique across teams")
            seen.update(team.api_key_sha256)
        return self

    def provider(self, name: str) -> ProviderConfig:
        return next(p for p in self.providers if p.name == name)


OVERRIDABLE_FIELDS = {
    "requests_per_minute",
    "tokens_per_minute",
    "daily_budget_usd",
    "budget_warning_ratio",
    "batch_reserve_ratio",
    "allowed_tiers",
    "enrichment",
}


def load_policy_file(path: str | Path, *, forbid_plaintext_keys: bool = False) -> Policy:
    with open(path, encoding="utf-8") as fh:
        raw = yaml.safe_load(fh)
    if forbid_plaintext_keys:
        offenders = [t.get("team_id", "?") for t in (raw or {}).get("teams", []) if t.get("api_keys")]
        if offenders:
            raise ValueError(
                f"plaintext api_keys are not allowed in production (teams: {offenders}); "
                "store api_key_sha256 hashes instead (python scripts/hash_key.py)"
            )
    return Policy.model_validate(raw)


class PolicyManager:
    """Holds the effective policy: YAML file + runtime overrides, with mtime-based hot reload."""

    def __init__(self, path: str | Path, policy: Policy | None = None, *, forbid_plaintext_keys: bool = False) -> None:
        self.forbid_plaintext_keys = forbid_plaintext_keys
        self.path = Path(path)
        self._base = policy or load_policy_file(self.path, forbid_plaintext_keys=forbid_plaintext_keys)
        self._mtime = self.path.stat().st_mtime if self.path.exists() else 0.0
        self._overrides: dict[str, dict[str, Any]] = {}
        self.version = 1
        self._rebuild()

    @property
    def policy(self) -> Policy:
        return self._effective

    def _rebuild(self) -> None:
        teams = []
        for team in self._base.teams:
            patch = self._overrides.get(team.team_id)
            teams.append(team.model_copy(update=patch) if patch else team)
        candidate = self._base.model_copy(update={"teams": teams})
        # Re-validate so overrides cannot reference unknown tiers etc.
        self._effective = Policy.model_validate(candidate.model_dump())
        self._by_hash = {h: t for t in self._effective.teams for h in t.api_key_sha256}
        self._by_id = {t.team_id: t for t in self._effective.teams}

    def team_by_key(self, api_key: str) -> TeamPolicy | None:
        return self._by_hash.get(hash_key(api_key))

    def team(self, team_id: str) -> TeamPolicy | None:
        return self._by_id.get(team_id)

    def set_overrides(self, overrides: dict[str, dict[str, Any]]) -> None:
        if overrides == self._overrides:
            return
        previous = self._overrides
        self._overrides = overrides
        try:
            self._rebuild()
        except Exception:
            self._overrides = previous
            raise
        self.version += 1

    def overrides(self) -> dict[str, dict[str, Any]]:
        return {k: dict(v) for k, v in self._overrides.items()}

    def validate_override(self, team_id: str, patch: dict[str, Any]) -> None:
        bad = set(patch) - OVERRIDABLE_FIELDS
        if bad:
            raise ValueError(f"fields not overridable at runtime: {sorted(bad)}")
        team = self._by_id[team_id]
        merged = team.model_copy(update={**self._overrides.get(team_id, {}), **patch})
        TeamPolicy.model_validate(merged.model_dump())
        unknown = set(merged.allowed_tiers) - set(self._effective.tiers)
        if unknown:
            raise ValueError(f"unknown tiers {sorted(unknown)}")

    def reload_if_changed(self) -> bool:
        """Reload the YAML file when its mtime changes. Invalid files are rejected; last good policy stays."""
        try:
            mtime = self.path.stat().st_mtime
        except FileNotFoundError:
            return False
        if mtime == self._mtime:
            return False
        try:
            base = load_policy_file(self.path, forbid_plaintext_keys=self.forbid_plaintext_keys)
        except Exception:
            log.exception("policy reload rejected; keeping last good policy", extra={"path": str(self.path)})
            self._mtime = mtime
            return False
        previous = self._base
        self._base, self._mtime = base, mtime
        try:
            self._rebuild()
        except Exception:
            log.exception("policy reload conflicts with runtime overrides; keeping last good policy")
            self._base = previous
            self._rebuild()
            return False
        self.version += 1
        log.info("policy reloaded", extra={"path": str(self.path), "version": self.version})
        return True
