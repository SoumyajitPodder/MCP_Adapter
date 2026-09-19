"""
agent.py

A deliberately simple stand-in for an LLM agent. It does NOT call a real
model -- it uses basic keyword matching to decide which tool to invoke
for a given natural-language task. That's enough to demonstrate the
pipeline honestly: a real LLM agent would replace only this reasoning
step, and everything downstream (gateway -> MCP -> adapter -> endpoint)
would be unaffected. That swap-ability is itself part of the point.

The agent only ever talks to the Gateway. It never knows about MCP, the
adapter, drivers, or the mock AT&T endpoint underneath -- that's the
whole premise of the pipeline.
"""

from gateway.gateway import Gateway, GatewayError


class Agent:
    def __init__(self):
        self.gateway = Gateway()

    def handle_task(self, task_description: str, trace=None, **kwargs) -> dict:
        """
        Given a plain-language task and any IDs it needs, decide which
        tool to call and call it through the gateway.

        kwargs carries whatever identifiers the task needs (customer_id,
        sku, account_id) -- standing in for slot-filling an LLM would
        normally do itself from the conversation.

        `trace` is an optional common.trace.Trace, threaded down through
        every layer so a caller (e.g. the web UI) can render exactly what
        happened, hop by hop.
        """
        print(f"[agent]       task: \"{task_description}\"")
        if trace:
            trace.step("agent", "task_received", {"task": task_description})

        tool_name, params = self._reason(task_description, kwargs)
        print(f"[agent]       decided to call tool '{tool_name}' with {params}")
        if trace:
            trace.step("agent", "tool_selected", {"tool_name": tool_name, "params": params})

        try:
            result = self.gateway.call_tool(tool_name, params, trace=trace)
        except GatewayError as exc:
            print(f"[agent]       gateway returned an error: {exc}")
            if trace:
                trace.step("agent", "task_failed", {"error": str(exc)}, status="error")
            return {"error": str(exc)}

        print(f"[agent]       got result back, task complete")
        if trace:
            trace.step("agent", "task_complete", {})
        return result

    @staticmethod
    def _reason(task_description: str, kwargs: dict):
        """Toy intent-matching. A real agent replaces this method only."""
        text = task_description.lower()

        if "ticket" in text or "repair" in text or "service" in text:
            return "get_service_ticket_status", {"ticket_id": kwargs["ticket_id"]}

        if "order" in text or "ship" in text or "track" in text or "deliver" in text:
            return "get_order_status", {"order_id": kwargs["order_id"]}

        # default: inventory/stock check
        return "check_device_inventory", {"sku": kwargs["sku"]}
