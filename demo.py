"""Hugging Face Space entrypoint: the gateway engine in front of a real model.

Providers (config/policy.space.yml), in fallback order for the default tier:
  1. Qwen2.5-1.5B-Instruct, run in this process on ZeroGPU (loaded below when a GPU runtime exists);
  2. Claude, only if the ANTHROPIC_API_KEY Space secret is set;
  3. a deterministic mock, so the demo keeps answering when everything real is unavailable.
"""

from __future__ import annotations

import logging
import threading
from collections import OrderedDict

import gradio as gr

from app.audit import AuditLog
from app.errors import GatewayError
from app.gateway import Gateway
from app.observability import Metrics
from app.policy import PolicyManager
from app.providers.local import LocalGeneration, register_local_model
from app.schemas import ChatMessage, ChatRequest
from app.stores import MemoryStore

log = logging.getLogger("demo")

policy = PolicyManager("config/policy.space.yml")
DEMO_TEAM = policy.team("demo")
PRIMARY = "zerogpu-qwen"
LOCAL_MODEL = policy.policy.provider(PRIMARY).model
MAX_NEW_TOKENS = 256
MAX_SESSIONS = 256

try:  # Present only on Hugging Face ZeroGPU hardware.
    import spaces
except ImportError:
    spaces = None

if spaces is not None:
    # ZeroGPU: place the model on "cuda" at import time; a real GPU is attached only while a
    # @spaces.GPU function runs.
    import torch
    from transformers import AutoModelForCausalLM, AutoTokenizer

    _tokenizer = AutoTokenizer.from_pretrained(LOCAL_MODEL)
    _model = AutoModelForCausalLM.from_pretrained(LOCAL_MODEL, dtype=torch.bfloat16).to("cuda")

    @spaces.GPU(duration=30)
    def _generate(messages: list[dict[str, str]], max_tokens: int) -> LocalGeneration:
        inputs = _tokenizer.apply_chat_template(
            messages, add_generation_prompt=True, return_dict=True, return_tensors="pt"
        ).to("cuda")
        output = _model.generate(**inputs, max_new_tokens=min(max_tokens, MAX_NEW_TOKENS), do_sample=False)
        prompt_len = inputs["input_ids"].shape[-1]
        new_tokens = output[0][prompt_len:]
        text = _tokenizer.decode(new_tokens, skip_special_tokens=True).strip()
        return LocalGeneration(text=text, prompt_tokens=int(prompt_len), completion_tokens=int(new_tokens.shape[-1]))

    register_local_model(LOCAL_MODEL, _generate)
else:
    log.warning("no ZeroGPU runtime; %s is skipped and traffic goes to the next provider", PRIMARY)


# One gateway per browser session, so one visitor's outage toggle and circuit state never affect
# another. Least recently used sessions are evicted.
#
# Rate limits and the daily budget live in ONE store shared by every session: they are a
# process-wide cap on spend. Per-session stores would let anyone reset the budget by opening a
# new tab. The store is in memory, so a Space restart resets it; for a paid provider also set a
# spend limit at the provider (e.g. in the Anthropic Console) as the hard backstop.
_shared_store = MemoryStore()
_sessions: OrderedDict[str, Gateway] = OrderedDict()
# Gradio runs sync handlers in worker threads and async ones on the event loop.
_sessions_lock = threading.Lock()


def _new_gateway() -> Gateway:
    return Gateway(policy, _shared_store, Metrics(), AuditLog(None))


def gateway_for(request: gr.Request | None) -> Gateway:
    key = getattr(request, "session_hash", None) or "local"
    with _sessions_lock:
        gateway = _sessions.get(key)
        if gateway is None:
            gateway = _sessions[key] = _new_gateway()
            if len(_sessions) > MAX_SESSIONS:
                _sessions.popitem(last=False)
        else:
            _sessions.move_to_end(key)
    return gateway


def health(gateway: Gateway) -> dict:
    report = gateway.health_report()
    return {
        name: {k: info[k] for k in ("status", "circuit", "error_rate", "p99_ms", "simulated_outage")}
        for name, info in report.items()
    }


async def send_request(prompt: str, tier: str, priority: str, request: gr.Request) -> tuple[str, str, dict, dict]:
    gateway = gateway_for(request)
    if not prompt or not prompt.strip():
        return "Enter a prompt to send through the gateway.", "", {}, health(gateway)
    req = ChatRequest(model=tier, messages=[ChatMessage(role="user", content=prompt.strip())], priority=priority)
    try:
        adm = await gateway.admit(DEMO_TEAM, req)
        resp, headers = await gateway.complete(adm)
    except GatewayError as exc:
        return f"Gateway error {exc.status_code} ({exc.code}): {exc.message}", "none", exc.headers, health(gateway)
    meta = resp.x_gateway
    served = f"{meta.provider} ({meta.served_model})" + ("  [fallback]" if meta.fallback_used else "")
    details = meta.model_dump() | {"usage": resp.usage.model_dump(), "headers": headers}
    return resp.choices[0].message.content, served, details, health(gateway)


def set_primary_outage(enabled: bool, request: gr.Request) -> tuple[str, dict]:
    gateway = gateway_for(request)
    gateway.faults.set(PRIMARY, outage=enabled)
    msg = (
        f"Outage on {PRIMARY}: requests retry, the circuit opens, and the next provider in the tier serves."
        if enabled
        else f"{PRIMARY} restored: after the cooldown, one half-open request closes the circuit."
    )
    return msg, health(gateway)


async def run_probe(request: gr.Request) -> dict:
    gateway = gateway_for(request)
    await gateway.probe_all()
    return health(gateway)


with gr.Blocks(title="Self-Healing LLM Gateway") as demo:
    gr.Markdown(
        f"""
    # Self-Healing LLM Gateway
    Requests go through the gateway engine: per-team rate limits and a daily budget, retries with
    backoff, circuit breakers, and tier-based failover.

    **Providers, in fallback order:** `{LOCAL_MODEL}` running in this Space on ZeroGPU, then Claude
    (only if the Space owner configured a key), then a deterministic fallback. The first request
    may take a few extra seconds while a GPU is attached. When your daily GPU quota is used up, the
    gateway fails over automatically.

    Source: [github.com/osho-m/self-healing-llm-gateway](https://github.com/osho-m/self-healing-llm-gateway)
    """
    )
    with gr.Row():
        with gr.Column(scale=2):
            prompt = gr.Textbox(
                label="Prompt", value="In two sentences, what does a circuit breaker do in a web service?", lines=4
            )
            tier = gr.Dropdown(["balanced", "economy", "premium"], value="balanced", label="Capability tier")
            priority = gr.Radio(["realtime", "batch"], value="realtime", label="Priority")
            send = gr.Button("Send through gateway", variant="primary")
            outage = gr.Checkbox(label=f"Simulate an outage of {PRIMARY}", value=False)
            probe = gr.Button("Run health probe")
        with gr.Column(scale=3):
            answer = gr.Textbox(label="Response", lines=6)
            served_by = gr.Textbox(label="Served by")
            metadata = gr.JSON(label="Routing, cost and budget metadata")
            status = gr.Textbox(label="Resilience status")
            health_view = gr.JSON(label="Provider health and circuit state", value=health(_new_gateway()))
    send.click(send_request, inputs=[prompt, tier, priority], outputs=[answer, served_by, metadata, health_view])
    outage.change(set_primary_outage, inputs=outage, outputs=[status, health_view])
    probe.click(run_probe, outputs=health_view)
    gr.Markdown(
        "Try it: send a request, enable the outage, and send again to see the retries, the open circuit "
        "and the fallback in the metadata. Each visitor's outage switch and circuits are isolated; rate "
        "limits and the daily budget are shared by all visitors."
    )


if __name__ == "__main__":
    demo.launch()
