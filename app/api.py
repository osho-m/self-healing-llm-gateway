"""HTTP routes."""

from __future__ import annotations

import hmac
from typing import Annotated

from fastapi import APIRouter, Depends, Header, Query, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse
from opentelemetry import trace
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from app.errors import GatewayError
from app.gateway import Gateway
from app.observability import tracer
from app.policy import TeamPolicy
from app.schemas import ChatRequest, ChatResponse, TeamPatch

router = APIRouter()


def get_gateway(request: Request) -> Gateway:
    return request.app.state.gateway


GatewayDep = Annotated[Gateway, Depends(get_gateway)]


async def require_team(
    gw: GatewayDep,
    x_api_key: Annotated[str | None, Header()] = None,
    authorization: Annotated[str | None, Header()] = None,
) -> TeamPolicy:
    with tracer.start_as_current_span("gateway.auth"):
        key = x_api_key or (
            authorization[7:] if authorization and authorization.lower().startswith("bearer ") else None
        )
        if not key:
            raise GatewayError(401, "missing_api_key", "Provide a team key via X-API-Key or Authorization: Bearer.")
        team = gw.policy_manager.team_by_key(key)
        if team is None:
            raise GatewayError(401, "invalid_api_key", "Unknown API key.")
        trace.get_current_span().set_attribute("gateway.team_id", team.team_id)
        return team


async def require_admin(
    request: Request,
    gw: GatewayDep,
    x_admin_key: Annotated[str | None, Header()] = None,
    x_api_key: Annotated[str | None, Header()] = None,
) -> str:
    """Admin routes need the dedicated admin key, or a team key whose policy role is ``admin``."""
    configured = request.app.state.settings.admin_api_key
    if x_admin_key and configured and hmac.compare_digest(x_admin_key.encode(), configured.encode()):
        return "admin-key"
    if x_api_key:
        team = gw.policy_manager.team_by_key(x_api_key)
        if team and team.role == "admin":
            return f"team:{team.team_id}"
        if team:
            raise GatewayError(403, "forbidden", "This key is not authorized for admin operations.")
    raise GatewayError(401, "admin_auth_required", "Provide X-Admin-Key (or an admin-role team key).")


TeamDep = Annotated[TeamPolicy, Depends(require_team)]
AdminDep = Annotated[str, Depends(require_admin)]


# ---------------------------------------------------------------- public


@router.post("/v1/chat/completions", response_model=ChatResponse, tags=["chat"])
async def chat_completions(body: ChatRequest, team: TeamDep, gw: GatewayDep, request: Request):
    span = trace.get_current_span()
    span.set_attributes(
        {"gateway.team_id": team.team_id, "gateway.model_requested": body.model, "gateway.stream": body.stream}
    )
    adm = await gw.admit(team, body)
    request.state.request_id = adm.request_id
    if body.stream:
        provider, events = await gw.stream(adm)
        headers = {
            **adm.rate_headers,
            "X-Request-ID": adm.request_id,
            "X-Gateway-Provider": provider,
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        }
        return StreamingResponse(events, media_type="text/event-stream", headers=headers)
    with tracer.start_as_current_span("gateway.route"):
        resp, headers = await gw.complete(adm)
    meta = resp.x_gateway
    span.set_attributes(
        {
            "gateway.model_served": meta.served_model,
            "gateway.provider": meta.provider,
            "gateway.tokens.input": resp.usage.prompt_tokens,
            "gateway.tokens.output": resp.usage.completion_tokens,
            "gateway.cost_usd": meta.cost_usd,
            "gateway.fallback_used": meta.fallback_used,
            "gateway.latency_ms": meta.latency_ms,
        }
    )
    return JSONResponse(resp.model_dump(), headers=headers)


@router.get("/v1/models", tags=["chat"])
async def list_models(team: TeamDep, gw: GatewayDep):
    return {
        "object": "list",
        "data": [
            {"id": tier, "object": "model", "owned_by": "gateway", "fallback_chain": gw.policy.tiers[tier]}
            for tier in team.allowed_tiers
        ],
    }


@router.get("/healthz", tags=["ops"])
async def healthz():
    return {"status": "ok"}


@router.get("/readyz", tags=["ops"])
async def readyz(gw: GatewayDep):
    store_ok = await gw.store.ping()
    report = gw.health_report()
    tiers_ok = {
        tier: any(report[p]["configured"] and report[p]["circuit"] != "open" for p in chain if p in report)
        for tier, chain in gw.policy.tiers.items()
    }
    ready = store_ok and all(tiers_ok.values())
    return JSONResponse(
        {"ready": ready, "store": store_ok, "tiers": tiers_ok, "policy_version": gw.policy_manager.version},
        status_code=200 if ready else 503,
    )


@router.get("/metrics", include_in_schema=False)
async def metrics(gw: GatewayDep):
    return Response(generate_latest(gw.metrics.registry), media_type=CONTENT_TYPE_LATEST)


# ---------------------------------------------------------------- admin

admin = APIRouter(prefix="/v1/admin", tags=["admin"])


def _provider_or_404(gw: Gateway, name: str) -> None:
    if name not in gw.providers:
        raise GatewayError(404, "provider_not_found", f"Unknown provider {name!r}.")


def _team_or_404(gw: Gateway, team_id: str) -> TeamPolicy:
    team = gw.policy_manager.team(team_id)
    if team is None:
        raise GatewayError(404, "team_not_found", f"Unknown team {team_id!r}.")
    return team


async def _team_status(gw: Gateway, team: TeamPolicy) -> dict:
    spent, reserved = await gw.store.budget_status(team.team_id)
    rate = await gw.store.rate_status(team.team_id, team.requests_per_minute, team.tokens_per_minute)
    util = spent / team.daily_budget_usd if team.daily_budget_usd else 1.0
    return {
        "team_id": team.team_id,
        "name": team.name,
        "role": team.role,
        "allowed_tiers": team.allowed_tiers,
        "limits": {
            "requests_per_minute": team.requests_per_minute,
            "tokens_per_minute": team.tokens_per_minute,
            "batch_reserve_ratio": team.batch_reserve_ratio,
        },
        "rate_limit_remaining": {k: round(v, 2) for k, v in rate.items()},
        "budget": {
            "daily_budget_usd": team.daily_budget_usd,
            "spent_today_usd": round(spent, 6),
            "reserved_usd": round(reserved, 6),
            "utilization": round(util, 4),
            "warning_ratio": team.budget_warning_ratio,
            "alert": "blocked" if util >= 1 else "warning" if util >= team.budget_warning_ratio else "ok",
        },
        "runtime_overrides": gw.policy_manager.overrides().get(team.team_id, {}),
    }


@admin.get("/providers")
async def providers(gw: GatewayDep, _: AdminDep):
    return gw.health_report()


@admin.post("/providers/{name}/simulate-outage")
async def simulate_outage(name: str, gw: GatewayDep, actor: AdminDep, enabled: bool = Query(...)):
    _provider_or_404(gw, name)
    gw.faults.set(name, outage=enabled)
    gw.audit.record(actor, "simulate_outage", name, {"enabled": enabled})
    return {"provider": name, "simulated_outage": enabled, "circuit": gw.breakers[name].current_state()}


@admin.post("/providers/{name}/inject-latency")
async def inject_latency(name: str, gw: GatewayDep, actor: AdminDep, ms: float = Query(..., ge=0, le=60_000)):
    _provider_or_404(gw, name)
    gw.faults.set(name, latency_ms=ms)
    gw.audit.record(actor, "inject_latency", name, {"ms": ms})
    return {"provider": name, "injected_latency_ms": ms}


@admin.post("/providers/{name}/probe")
async def probe(name: str, gw: GatewayDep, _: AdminDep):
    _provider_or_404(gw, name)
    await gw.probe(name)
    return gw.health_report()[name]


@admin.get("/teams")
async def teams(gw: GatewayDep, _: AdminDep):
    return [await _team_status(gw, t) for t in gw.policy.teams]


@admin.get("/teams/{team_id}/status")
async def team_status(team_id: str, gw: GatewayDep, _: AdminDep):
    return await _team_status(gw, _team_or_404(gw, team_id))


@admin.patch("/teams/{team_id}")
async def patch_team(team_id: str, patch: TeamPatch, gw: GatewayDep, actor: AdminDep):
    _team_or_404(gw, team_id)
    changes = patch.model_dump(exclude_none=True)
    if not changes:
        raise GatewayError(400, "empty_patch", "Provide at least one field to change.")
    try:
        gw.policy_manager.validate_override(team_id, changes)
    except ValueError as exc:
        raise GatewayError(422, "invalid_override", str(exc)) from exc
    await gw.store.set_overrides(team_id, changes)
    await gw.refresh_policy()
    gw.audit.record(actor, "update_team", team_id, changes)
    return await _team_status(gw, _team_or_404(gw, team_id))


@admin.delete("/teams/{team_id}/overrides")
async def clear_overrides(team_id: str, gw: GatewayDep, actor: AdminDep):
    _team_or_404(gw, team_id)
    await gw.store.set_overrides(team_id, None)
    await gw.refresh_policy()
    gw.audit.record(actor, "clear_overrides", team_id)
    return await _team_status(gw, _team_or_404(gw, team_id))


@admin.get("/audit")
async def audit(gw: GatewayDep, _: AdminDep, limit: int = Query(100, ge=1, le=1000)):
    return gw.audit.recent(limit)


@admin.post("/policy/reload")
async def reload_policy(gw: GatewayDep, actor: AdminDep):
    changed = gw.policy_manager.reload_if_changed()
    await gw.refresh_policy()
    gw.audit.record(actor, "reload_policy", str(gw.policy_manager.path), {"changed": changed})
    return {"changed": changed, "version": gw.policy_manager.version}


router.include_router(admin)
