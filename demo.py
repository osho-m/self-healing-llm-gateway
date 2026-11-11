"""Hugging Face Gradio Space entrypoint; no Docker or external API key required."""
from __future__ import annotations

import gradio as gr

from space_gateway import gateway


def send_request(prompt: str, tier: str) -> tuple[str, dict]:
    if not prompt or not prompt.strip():
        return "Enter a prompt to send through the gateway.", {}
    try:
        return gateway.complete(prompt.strip(), tier)
    except (RuntimeError, ValueError) as exc:
        return f"Gateway error: {exc}", gateway.health()


def set_primary_outage(enabled: bool) -> tuple[str, dict]:
    return gateway.set_outage(enabled), gateway.health()


with gr.Blocks(title="Self-Healing LLM Gateway", theme=gr.themes.Soft()) as demo:
    gr.Markdown("""
    # 🛡️ Self-Healing LLM Gateway
    A no-key production-infrastructure demo. Requests are rate-limited, budgeted, retried,
    and automatically moved to a healthy provider when the primary fails.
    """)
    with gr.Row():
        with gr.Column(scale=2):
            prompt = gr.Textbox(label="Prompt", value="Explain how a circuit breaker protects an LLM application.", lines=5)
            tier = gr.Dropdown(["economy", "balanced", "premium"], value="balanced", label="Capability tier")
            send = gr.Button("Send through gateway", variant="primary")
            outage = gr.Checkbox(label="Simulate mock-primary outage", value=False)
            refresh = gr.Button("Refresh health")
        with gr.Column(scale=3):
            answer = gr.Textbox(label="Gateway response", lines=7)
            metadata = gr.JSON(label="Routing and cost metadata")
            status = gr.Textbox(label="Resilience status")
            health_view = gr.JSON(label="Provider health / circuit state", value=gateway.health())
    send.click(send_request, inputs=[prompt, tier], outputs=[answer, metadata])
    outage.change(set_primary_outage, inputs=outage, outputs=[status, health_view])
    refresh.click(gateway.health, outputs=health_view)
    gr.Markdown("**Demo flow:** send a request → enable the outage → send again → observe `mock-secondary` and `fallback_used: true`. No external model API key is used.")


if __name__ == "__main__":
    demo.launch()
