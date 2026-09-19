"""
mock_att_api.py

Simulates a legacy AT&T-style operations backend exposed as a REST API,
covering three domains: inventory, service tickets, and order management.
This represents the "endpoint" layer at the very bottom of the pipeline:

    Agent -> Gateway -> MCP -> Adapter -> Endpoint (this file)

It is intentionally a plain, unversioned REST service -- the kind of thing
a real telecom legacy system might expose. It knows nothing about agents,
MCP, or semantic contracts. It just serves data.

DRIFT SIMULATION (for future use, not active by default)
----------------------------------------------------------
Set the environment variable ATT_MOCK_DRIFT_MODE to change response shape,
to give the future "contract testing" and "golden-task regression" work
something real to detect. Not used by anything yet -- just wired in so v1
of those capabilities has a lever to pull without touching this file again.

    none    (default) - stable, well-formed responses
    rename  - renames a field in each response (simulates a breaking
              upstream change, e.g. "quantity_available" -> "qty_avail")
    delay   - adds artificial latency (simulates a degrading backend,
              useful later for testing gateway fallback/circuit breakers)
    error   - randomly 500s ~30% of the time (simulates flaky backend)
"""

import os
import random
import time

from flask import Flask, jsonify

app = Flask(__name__)

DRIFT_MODE = os.environ.get("ATT_MOCK_DRIFT_MODE", "none")

# --- Fake legacy datastore -------------------------------------------------

INVENTORY = {
    "SKU-IP15PM": {
        "sku": "SKU-IP15PM",
        "device_name": "iPhone 15 Pro Max",
        "quantity_available": 42,
        "warehouse": "DFW-01",
    },
    "SKU-GS24U": {
        "sku": "SKU-GS24U",
        "device_name": "Samsung Galaxy S24 Ultra",
        "quantity_available": 0,
        "warehouse": "DFW-01",
    },
    "SKU-PXL9PR": {
        "sku": "SKU-PXL9PR",
        "device_name": "Pixel 9 Pro",
        "quantity_available": 17,
        "warehouse": "ATL-02",
    },
}

SERVICE_TICKETS = {
    "TICKET-5001": {
        "ticket_id": "TICKET-5001",
        "issue_type": "Device won't power on",
        "sku": "SKU-IP15PM",
        "status": "in_progress",
        "priority": "high",
        "technician_assigned": "M. Alvarez",
        "opened_date": "2026-09-10",
    },
    "TICKET-5002": {
        "ticket_id": "TICKET-5002",
        "issue_type": "Screen replacement",
        "sku": "SKU-GS24U",
        "status": "resolved",
        "priority": "medium",
        "technician_assigned": "D. Chen",
        "opened_date": "2026-09-02",
    },
    "TICKET-5003": {
        "ticket_id": "TICKET-5003",
        "issue_type": "Battery draining fast",
        "sku": "SKU-PXL9PR",
        "status": "open",
        "priority": "low",
        "technician_assigned": None,
        "opened_date": "2026-09-15",
    },
}

ORDERS = {
    "ORDER-8001": {
        "order_id": "ORDER-8001",
        "sku": "SKU-IP15PM",
        "quantity": 2,
        "status": "shipped",
        "carrier": "FedEx",
        "tracking_number": "784512339981",
        "expected_delivery": "2026-09-19",
    },
    "ORDER-8002": {
        "order_id": "ORDER-8002",
        "sku": "SKU-GS24U",
        "quantity": 5,
        "status": "backordered",
        "carrier": None,
        "tracking_number": None,
        "expected_delivery": None,
    },
    "ORDER-8003": {
        "order_id": "ORDER-8003",
        "sku": "SKU-PXL9PR",
        "quantity": 1,
        "status": "delivered",
        "carrier": "UPS",
        "tracking_number": "1Z999AA10123456784",
        "expected_delivery": "2026-09-14",
    },
}


def _apply_drift(payload: dict) -> dict:
    """Apply the configured drift mode to a response payload."""
    if DRIFT_MODE == "delay":
        time.sleep(2.5)
    elif DRIFT_MODE == "error" and random.random() < 0.3:
        return None  # signals caller to 500
    elif DRIFT_MODE == "rename":
        payload = dict(payload)
        if "quantity_available" in payload:
            payload["qty_avail"] = payload.pop("quantity_available")
        if "status" in payload:
            payload["current_status"] = payload.pop("status")
        if "tracking_number" in payload:
            payload["tracking_id"] = payload.pop("tracking_number")
    return payload


@app.route("/api/v1/inventory/<sku>", methods=["GET"])
def get_inventory(sku):
    record = INVENTORY.get(sku)
    if not record:
        return jsonify({"error": "sku not found"}), 404
    result = _apply_drift(record)
    if result is None:
        return jsonify({"error": "internal legacy system error"}), 500
    return jsonify(result), 200


@app.route("/api/v1/service-tickets/<ticket_id>", methods=["GET"])
def get_service_ticket(ticket_id):
    record = SERVICE_TICKETS.get(ticket_id)
    if not record:
        return jsonify({"error": "service ticket not found"}), 404
    result = _apply_drift(record)
    if result is None:
        return jsonify({"error": "internal legacy system error"}), 500
    return jsonify(result), 200


@app.route("/api/v1/orders/<order_id>", methods=["GET"])
def get_order(order_id):
    record = ORDERS.get(order_id)
    if not record:
        return jsonify({"error": "order not found"}), 404
    result = _apply_drift(record)
    if result is None:
        return jsonify({"error": "internal legacy system error"}), 500
    return jsonify(result), 200


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok", "drift_mode": DRIFT_MODE}), 200


if __name__ == "__main__":
    print(f"[mock-att-api] starting on :5001 (drift_mode={DRIFT_MODE})")
    app.run(host="0.0.0.0", port=5001)
