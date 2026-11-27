"""Hugging Face Space entrypoint: the gateway in front of three open-source models.

All three models run inside this Space on ZeroGPU (config/policy.space.yml):
  economy  -> SmolLM2-1.7B-Instruct
  balanced -> Qwen2.5-1.5B-Instruct
  premium  -> Phi-3.5-mini-instruct
Each tier falls back to the other two models, then to a deterministic mock.
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
LOCAL = [p for p in policy.policy.providers if p.kind == "local"]  # provider configs, in policy order
MAX_NEW_TOKENS = 256
MAX_SESSIONS = 256

try:  # Present only on Hugging Face ZeroGPU hardware.
    import spaces
except ImportError:
    spaces = None

if spaces is not None:
    # ZeroGPU: models are placed on "cuda" at startup; a real GPU is attached only while a
    # @spaces.GPU function runs.
    import torch
    from transformers import AutoModelForCausalLM, AutoTokenizer

    _loaded = {}
    for cfg in LOCAL:
        tokenizer = AutoTokenizer.from_pretrained(cfg.model)
        model = AutoModelForCausalLM.from_pretrained(cfg.model, dtype=torch.bfloat16).to("cuda")
        _loaded[cfg.model] = (tokenizer, model)

    @spaces.GPU(duration=30)
    def _run(model_id: str, messages: list[dict[str, str]], max_tokens: int) -> LocalGeneration:
        tokenizer, model = _loaded[model_id]
        inputs = tokenizer.apply_chat_template(
            messages, add_generation_prompt=True, return_dict=True, return_tensors="pt"
        ).to("cuda")
        output = model.generate(**inputs, max_new_tokens=min(max_tokens, MAX_NEW_TOKENS), do_sample=False)
        prompt_len = inputs["input_ids"].shape[-1]
        new_tokens = output[0][prompt_len:]
        text = tokenizer.decode(new_tokens, skip_special_tokens=True).strip()
        return LocalGeneration(text=text, prompt_tokens=int(prompt_len), completion_tokens=int(new_tokens.shape[-1]))

    for cfg in LOCAL:
        register_local_model(cfg.model, lambda messages, max_tokens, m=cfg.model: _run(m, messages, max_tokens))
else:
    log.warning("no ZeroGPU runtime: the local models are skipped and the mock answers")


# One gateway per browser session, so one visitor's outage switches and circuit state never
# affect another. The least recently used sessions are evicted.
#
# Rate limits and the daily budget live in ONE store shared by every session, so they cap the
# whole Space. Opening a new tab cannot reset them. The store is in memory: a restart resets it.
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
        return "Type a prompt first.", "", {}, health(gateway)
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


def set_outages(down: list[str], request: gr.Request) -> tuple[str, dict]:
    gateway = gateway_for(request)
    for cfg in LOCAL:
        gateway.faults.set(cfg.name, outage=cfg.name in down)
    if down:
        msg = f"Simulated outage: {', '.join(down)}. Requests retry, the circuit opens, and the next model serves."
    else:
        msg = "All models restored. After the cooldown, one test request closes each open circuit."
    return msg, health(gateway)


async def run_probe(request: gr.Request) -> dict:
    gateway = gateway_for(request)
    await gateway.probe_all()
    return health(gateway)


with gr.Blocks(title="Self-Healing LLM Gateway") as demo:
    gr.Markdown(
        """
    # Self-Healing LLM Gateway
    Every request goes through the gateway. It checks your rate limit, picks a model for the tier
    you chose, retries if the model fails, and switches to the next model when one is down.

    **Three open-source models run in this Space:** SmolLM2-1.7B (economy), Qwen2.5-1.5B (balanced)
    and Phi-3.5-mini (premium). Each tier falls back to the other two. No paid API is used.

    The first request can take a few seconds while a GPU is attached. If your free GPU time runs
    out, the gateway fails over on its own.

    Source: [github.com/osho-m/self-healing-llm-gateway](https://github.com/osho-m/self-healing-llm-gateway)
    """
    )
    with gr.Row():
        with gr.Column(scale=2):
            prompt = gr.Textbox(
                label="Prompt", value="In two sentences, what does a circuit breaker do in a web service?", lines=4
            )
            tier = gr.Dropdown(["balanced", "economy", "premium"], value="balanced", label="Tier")
            priority = gr.Radio(["realtime", "batch"], value="realtime", label="Priority")
            send = gr.Button("Send through gateway", variant="primary")
            outages = gr.CheckboxGroup([cfg.name for cfg in LOCAL], label="Simulate an outage of")
            probe = gr.Button("Run health check")
        with gr.Column(scale=3):
            answer = gr.Textbox(label="Response", lines=6)
            served_by = gr.Textbox(label="Served by")
            metadata = gr.JSON(label="Routing, retries and usage")
            status = gr.Textbox(label="What is happening")
            health_view = gr.JSON(label="Model health and circuit state", value=health(_new_gateway()))
    send.click(send_request, inputs=[prompt, tier, priority], outputs=[answer, served_by, metadata, health_view])
    outages.change(set_outages, inputs=outages, outputs=[status, health_view])
    probe.click(run_probe, outputs=health_view)
    gr.Markdown(
        "Try it: send a request, tick the model your tier starts with, and send again. The metadata shows "
        "the failed attempts and which model answered instead. Your outage switches only affect you; rate "
        "limits are shared by everyone using the Space."
    )


if __name__ == "__main__":
    demo.launch()
