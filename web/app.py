"""
web/app.py

Web UI for the sandbox: a chat-style agent interface plus a live
"System Internals" panel that shows exactly what the pipeline did for
each request -- which layer handled it, which driver and API version
served it, timing, and status.

This does NOT reimplement anything. It calls the exact same Agent /
Gateway / MCP / Adapter code the CLI demo (demo/run_demo.py) uses --
the UI is just a different way of driving and observing the same
pipeline.

CAPABILITIES PANEL
-------------------
CAPABILITIES below is the single source of truth for "what's built vs.
what's planned" in the MCP layer and beyond. As each of the roadmap
items from the README gets implemented, flip its "status" here from
"planned" to "implemented" -- the UI picks it up automatically, no
frontend changes required.

Run:
    # terminal 1
    python endpoint/mock_att_api.py
    # terminal 2
    python web/app.py
    # then open http://127.0.0.1:5050
"""

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import requests
from flask import Flask, jsonify, render_template, request

from agent.agent import Agent
from common.trace import Trace

app = Flask(__name__)
agent = Agent()

ENDPOINT_HEALTH_URL = "http://127.0.0.1:5001/health"

# --- Capability roadmap: single source of truth for the UI panel ----------

CAPABILITIES = [
    {"id": "api_driver", "layer": "Adapter", "name": "API driver (inventory / service / orders)", "status": "implemented"},
    {"id": "tool_registry", "layer": "MCP", "name": "Tool discovery & dispatch", "status": "implemented"},
    {"id": "versioned_contracts", "layer": "MCP", "name": "Versioned tool contracts (v1/v2 shims)", "status": "planned"},
    {"id": "drift_absorption", "layer": "MCP", "name": "Drift absorption / stable output shape", "status": "planned"},
    {"id": "lifecycle_routing", "layer": "Gateway", "name": "Version lifecycle routing (canary \u2192 primary \u2192 retired)", "status": "planned"},
    {"id": "contract_testing", "layer": "MCP", "name": "Contract testing in CI", "status": "planned"},
    {"id": "golden_task_regression", "layer": "Agent", "name": "Golden-task regression suite", "status": "planned"},
    {"id": "idempotency_keys", "layer": "Adapter", "name": "Idempotency keys for writes", "status": "planned"},
    {"id": "scoped_credentials", "layer": "Gateway", "name": "Scoped least-privilege credentials", "status": "planned"},
    {"id": "correlation_ids", "layer": "Gateway", "name": "Correlation-ID tracing", "status": "planned"},
    {"id": "circuit_breakers", "layer": "Gateway", "name": "Retries / circuit breakers / fallback", "status": "planned"},
    {"id": "db_driver", "layer": "Adapter", "name": "Database driver", "status": "planned"},
    {"id": "file_driver", "layer": "Adapter", "name": "File / batch driver", "status": "planned"},
    {"id": "queue_driver", "layer": "Adapter", "name": "Message queue driver", "status": "planned"},
    {"id": "rpa_driver", "layer": "Adapter", "name": "RPA / UI automation driver", "status": "planned"},
]

# --- Very small helper to pull demo IDs out of free-text chat input -------

ID_PATTERNS = {
    "sku": re.compile(r"SKU-[A-Z0-9]+", re.I),
    "ticket_id": re.compile(r"TICKET-\d+", re.I),
    "order_id": re.compile(r"ORDER-\d+", re.I),
}

DEFAULT_IDS = {"sku": "SKU-IP15PM", "ticket_id": "TICKET-5001", "order_id": "ORDER-8001"}


def extract_ids(text: str) -> dict:
    """Pull whatever IDs are mentioned in free text; fall back to demo
    defaults for the rest so the agent's toy reasoning always has what it
    needs, regardless of which single ID the user actually typed."""
    found = {}
    for key, pattern in ID_PATTERNS.items():
        match = pattern.search(text)
        if match:
            found[key] = match.group(0).upper()
    return {**DEFAULT_IDS, **found}


def summarize(result: dict) -> str:
    """Turn a raw tool result into an agent-sounding chat reply."""
    if "error" in result:
        first_line = result["error"].splitlines()[0]
        return f"I wasn't able to complete that -- {first_line}"

    if "device_name" in result:
        qty = result.get("quantity_available", result.get("qty_avail"))
        if qty:
            return f"{result['device_name']}: {qty} unit(s) available at {result.get('warehouse', 'the warehouse')}."
        return f"{result['device_name']} is currently out of stock."

    if "issue_type" in result:
        status = result.get("status", result.get("current_status", "unknown"))
        tech = result.get("technician_assigned")
        tech_note = f", assigned to {tech}" if tech else ", not yet assigned to a technician"
        return f"Ticket {result['ticket_id']} ({result['issue_type']}) is {status}{tech_note}."

    if "carrier" in result or "tracking_number" in result or "expected_delivery" in result:
        status = result.get("status", result.get("current_status", "unknown"))
        if status in ("shipped", "delivered"):
            tracking = result.get("tracking_number", result.get("tracking_id"))
            carrier = result.get("carrier", "carrier")
            return f"Order {result['order_id']} is {status} via {carrier}, tracking {tracking}."
        return f"Order {result['order_id']} is currently {status}."

    return "Done -- see the trace panel for the full result."


@app.route("/")
def index():
    endpoint_up = True
    try:
        requests.get(ENDPOINT_HEALTH_URL, timeout=1)
    except requests.exceptions.RequestException:
        endpoint_up = False
    return render_template("index.html", endpoint_up=endpoint_up)


@app.route("/api/capabilities")
def capabilities():
    return jsonify(CAPABILITIES)


@app.route("/api/task", methods=["POST"])
def task():
    body = request.get_json(force=True, silent=True) or {}
    text = (body.get("task") or "").strip()
    if not text:
        return jsonify({"error": "empty task"}), 400

    ids = extract_ids(text)
    trace = Trace()
    result = agent.handle_task(text, trace=trace, **ids)

    return jsonify(
        {
            "reply": summarize(result),
            "result": result,
            "trace": trace.to_list(),
            "params_used": ids,
        }
    )


if __name__ == "__main__":
    print("[web] starting on http://127.0.0.1:5050")
    print("[web] make sure endpoint/mock_att_api.py is running on :5001")
    app.run(host="0.0.0.0", port=5050, debug=False)
