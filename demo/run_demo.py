"""
run_demo.py

Drives a few example tasks all the way through the pipeline:

    Agent -> Gateway -> MCP -> Adapter (API driver) -> Mock AT&T endpoint

Covers the three domains: inventory, service tickets, and order
management.

Run the mock endpoint first, in a separate terminal:
    python endpoint/mock_att_api.py

Then run this:
    python demo/run_demo.py

Each layer prints a line as the request/response passes through it, so
you can see the hop-by-hop flow. Try re-running with drift injected:

    ATT_MOCK_DRIFT_MODE=rename python endpoint/mock_att_api.py

...and watch the raw result change shape with nothing catching it -- that
gap is exactly what the semantic layer / contract testing work (deferred
out of v1) is meant to close.
"""

import json
import sys
from pathlib import Path

# allow running as `python demo/run_demo.py` from the project root
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent.agent import Agent


def run_task(agent: Agent, label: str, task: str, **kwargs):
    print("\n" + "=" * 70)
    print(f"TASK: {label}")
    print("=" * 70)
    result = agent.handle_task(task, **kwargs)
    print("-" * 70)
    print("FINAL RESULT:")
    print(json.dumps(result, indent=2))


def main():
    agent = Agent()

    print("Discovering available tools (agent -> gateway -> MCP)...")
    tools = agent.gateway.list_tools()
    for name, spec in tools.items():
        print(f"  - {name}: {spec['description']}")

    run_task(
        agent,
        "Check device stock",
        "Do we have any of this phone in stock?",
        sku="SKU-GS24U",
    )

    run_task(
        agent,
        "Check a service ticket",
        "What's the status on this repair ticket?",
        ticket_id="TICKET-5001",
    )

    run_task(
        agent,
        "Check an order's shipping status",
        "Can you track this order for me?",
        order_id="ORDER-8001",
    )

    run_task(
        agent,
        "Look up an order that doesn't exist (error path)",
        "Can you track this order for me?",
        order_id="ORDER-9999",
    )

    print("\n" + "=" * 70)
    print("Demo complete.")
    print("=" * 70)


if __name__ == "__main__":
    main()
