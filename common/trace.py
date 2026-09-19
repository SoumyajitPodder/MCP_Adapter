"""
trace.py

A tiny structured tracer, threaded through every layer of the pipeline as
an optional argument. Each layer calls `trace.step(...)` as a request
passes through it. The web UI renders the resulting list as the
"System Internals" panel.

Deliberately generic (layer, action, detail, status) rather than
hard-coded to today's pipeline shape -- when versioned contracts,
semantic validation, or new driver types get added later, they just call
`trace.step(...)` with their own layer/action names and the UI renders
them automatically. No UI changes required to show new pipeline stages.
"""

import time


class Trace:
    def __init__(self):
        self.steps = []
        self._start = time.time()

    def step(self, layer: str, action: str, detail: dict = None, status: str = "ok"):
        self.steps.append(
            {
                "t_ms": round((time.time() - self._start) * 1000, 1),
                "layer": layer,
                "action": action,
                "detail": detail or {},
                "status": status,
            }
        )

    def to_list(self):
        return self.steps
