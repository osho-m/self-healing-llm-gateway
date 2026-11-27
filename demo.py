"""Hugging Face Gradio Space entrypoint. Runs the real gateway engine in-process; no Docker or API key needed."""

from __future__ import annotations

import gradio as gr

from app.audit import AuditLog
from app.errors import GatewayError
from app.gateway import Gateway
from app.observability import Metrics
from app.policy import PolicyManager
from app.schemas import ChatMessage, ChatRequest
from app.stores import MemoryStore

try:  # Present only on Hugging Face ZeroGPU hardware.
    import spaces
except ImportError:
    spaces = None

if spaces is not None:
    # ZeroGPU refuses to start a Space without at least one @spaces.GPU function. The gateway is
    # CPU-only (it routes to providers, it does not run models), so this is never called.
    @spaces.GPU(duration=1)
    def _zerogpu_requires_a_gpu_function() -> None:
        return None


policy = PolicyManager("config/policy.example.yml")
gateway = Gateway(policy, MemoryStore(), Metrics(), AuditLog(None))
DEMO_TEAM = policy.team("demo")
PRIMARY = "mock-primary"


def health() -> dict:
    report = gateway.health_report()
    return {
        name: {k: info[k] for k in ("status", "circuit", "error_rate", "p99_ms", "simulated_outage")}
        for name, info in report.items()
    }


async def send_request(prompt: str, tier: str, priority: str) -> tuple[str, dict, dict]:
    if not prompt or not prompt.strip():
        return "Enter a prompt to send through the gateway.", {}, health()
    req = ChatRequest(model=tier, messages=[ChatMessage(role="user", content=prompt.strip())], priority=priority)
    try:
        adm = await gateway.admit(DEMO_TEAM, req)
        resp, headers = await gateway.complete(adm)
    except GatewayError as exc:
        return f"Gateway error {exc.status_code} ({exc.code}): {exc.message}", exc.headers, health()
    meta = resp.x_gateway.model_dump() | {"usage": resp.usage.model_dump(), "headers": headers}
    return resp.choices[0].message.content, meta, health()


def set_primary_outage(enabled: bool) -> tuple[str, dict]:
    gateway.faults.set(PRIMARY, outage=enabled)
    msg = (
        "OUTAGE ENABLED: requests retry with backoff, the circuit opens, traffic fails over."
        if enabled
        else "Primary restored: after the cooldown the circuit goes half-open and one probe closes it."
    )
    return msg, health()


async def run_probe() -> dict:
    await gateway.probe_all()
    return health()


with gr.Blocks(title="Self-Healing LLM Gateway") as demo:
    gr.Markdown(
        """
    # Self-Healing LLM Gateway
    The same engine as the FastAPI service: per-team rate limits and budgets, retries with
    exponential backoff, circuit breakers, and tier-based failover. Providers here are deterministic
    in-process mocks, so no external API key is used.
    """
    )
    with gr.Row():
        with gr.Column(scale=2):
            prompt = gr.Textbox(
                label="Prompt", value="Explain how a circuit breaker protects an LLM application.", lines=5
            )
            tier = gr.Dropdown(["economy", "balanced", "premium"], value="balanced", label="Capability tier")
            priority = gr.Radio(["realtime", "batch"], value="realtime", label="Priority")
            send = gr.Button("Send through gateway", variant="primary")
            outage = gr.Checkbox(label=f"Simulate {PRIMARY} outage", value=False)
            probe = gr.Button("Run health probe")
        with gr.Column(scale=3):
            answer = gr.Textbox(label="Gateway response", lines=6)
            metadata = gr.JSON(label="Routing, cost and budget metadata")
            status = gr.Textbox(label="Resilience status")
            health_view = gr.JSON(label="Provider health / circuit state", value=health())
    send.click(send_request, inputs=[prompt, tier, priority], outputs=[answer, metadata, health_view])
    outage.change(set_primary_outage, inputs=outage, outputs=[status, health_view])
    probe.click(run_probe, outputs=health_view)
    gr.Markdown(
        "**Demo flow:** send a request, enable the outage, send again, then look for `mock-secondary` "
        "and `fallback_used: true`. Disable the outage, wait 15 s, and run a probe to watch the circuit close."
    )


if __name__ == "__main__":
    demo.launch()
