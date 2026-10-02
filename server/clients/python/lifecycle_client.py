"""Client for the lifecycle controller server. Standard library only.

    from lifecycle_client import LifecycleClient, LifecycleError

    agent = LifecycleClient("http://127.0.0.1:8787", token=AGENT_TOKEN)
    order = agent.call("get_order_status", {"order_id": "ORDER-8001"})["data"][0]

    ops = LifecycleClient("http://127.0.0.1:8787", token=ADMIN_TOKEN)
    for review in ops.reviews():        # what needs a person
        ops.approve(review["id"])

Tool calls raise LifecycleError on anything but success; `.code` is the stable
machine-readable reason (NOT_FOUND, BAD_REQUEST, DRIFT_BLOCKED,
UPSTREAM_UNAVAILABLE, CONTRACT_SUNSET, RATE_LIMITED, ...), `.status` the HTTP
status, and for DRIFT_BLOCKED `.review` is the id of the review a person
needs to look at. Pass raise_on_error=False to get the error body instead.
"""
import json
import http.client
import urllib.error
import urllib.parse
import urllib.request


class LifecycleError(Exception):
    def __init__(self, status, code, message, details=None, review=None):
        super().__init__(f"{code} (HTTP {status}): {message}")
        self.status, self.code, self.message, self.details, self.review = status, code, message, details, review


class LifecycleClient:
    def __init__(self, base_url, token=None, timeout=15.0):
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout = timeout

    # -- transport --------------------------------------------------------
    def _request(self, method, path, body=None):
        data = json.dumps(body).encode() if body is not None else None
        headers = {"Accept": "application/json"}
        if data is not None:
            headers["Content-Type"] = "application/json"
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        req = urllib.request.Request(self.base_url + path, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return resp.status, json.loads(resp.read() or b"null")
        except urllib.error.HTTPError as e:
            raw = e.read()
            try:
                return e.code, json.loads(raw)
            except ValueError:
                return e.code, {"error": {"code": "HTTP_ERROR", "message": raw.decode(errors="replace")[:200]}}

    def _ok(self, method, path, body=None):
        status, payload = self._request(method, path, body)
        if status >= 400:
            err = (payload or {}).get("error", {})
            raise LifecycleError(status, err.get("code", "HTTP_ERROR"), err.get("message", ""), err.get("details"), err.get("review"))
        return payload

    # -- agents -----------------------------------------------------------
    def tools(self):
        return self._ok("GET", "/v1/tools")["tools"]

    def tool(self, name):
        return self._ok("GET", "/v1/tools/" + urllib.parse.quote(name, safe=""))

    def call(self, tool, args=None, raise_on_error=True):
        status, payload = self._request("POST", "/v1/tools/" + urllib.parse.quote(tool, safe="") + "/call", {"args": args or {}})
        if status >= 400 and raise_on_error:
            err = (payload or {}).get("error", {})
            raise LifecycleError(status, err.get("code", "HTTP_ERROR"), err.get("message", ""), err.get("details"), err.get("review"))
        return payload

    def health(self):
        return self._ok("GET", "/healthz")

    def ready(self):
        return self._request("GET", "/readyz")[1]

    # -- people (admin token) ---------------------------------------------
    def bindings(self):
        return self._ok("GET", "/v1/bindings")["bindings"]

    def binding(self, binding_id):
        return self._ok("GET", "/v1/bindings/" + urllib.parse.quote(binding_id, safe=""))

    def add_binding(self, definition):
        return self._ok("POST", "/v1/bindings", definition)

    def remove_binding(self, binding_id):
        return self._ok("DELETE", "/v1/bindings/" + urllib.parse.quote(binding_id, safe=""))

    def run_batch(self):
        return self._ok("POST", "/v1/batch")

    def reviews(self, status="open", binding=None):
        q = {"status": status}
        if binding:
            q["binding"] = binding
        return self._ok("GET", "/v1/reviews?" + urllib.parse.urlencode(q))["reviews"]

    def review(self, review_id):
        return self._ok("GET", "/v1/reviews/" + review_id)

    def approve(self, review_id):
        return self._ok("POST", f"/v1/reviews/{review_id}/approve")

    def reject(self, review_id):
        return self._ok("POST", f"/v1/reviews/{review_id}/reject")

    def choose(self, review_id, index, value):
        return self._ok("POST", f"/v1/reviews/{review_id}/choices", {"index": index, "value": value})

    def preview_migration(self, binding_id, version, choices=None):
        return self._ok("POST", f"/v1/bindings/{binding_id}/migrations/preview", {"version": version, "choices": choices} if choices else {"version": version})

    def begin_migration(self, binding_id, version, choices=None):
        return self._ok("POST", f"/v1/bindings/{binding_id}/migrations", {"version": version, "choices": choices} if choices else {"version": version})

    def promote(self, binding_id, adapter_id):
        return self._ok("POST", f"/v1/bindings/{binding_id}/adapters/{adapter_id}/promote")

    def retire(self, binding_id, adapter_id):
        return self._ok("POST", f"/v1/bindings/{binding_id}/adapters/{adapter_id}/retire")

    def deprecate_contract(self, binding_id):
        return self._ok("POST", f"/v1/bindings/{binding_id}/contract/deprecate")

    def audit(self, limit=100, **filters):
        q = {"limit": limit, **{k: v for k, v in filters.items() if v is not None}}
        return self._ok("GET", "/v1/audit?" + urllib.parse.urlencode(q))["entries"]

    def events(self):
        """Yield (event, data) from the server-sent event stream until the connection closes."""
        u = urllib.parse.urlparse(self.base_url)
        conn = http.client.HTTPConnection(u.hostname, u.port, timeout=self.timeout)
        conn.request("GET", "/v1/events", headers={"Authorization": f"Bearer {self.token}", "Accept": "text/event-stream"})
        resp = conn.getresponse()
        if resp.status != 200:
            raise LifecycleError(resp.status, "HTTP_ERROR", resp.read().decode(errors="replace")[:200])
        event = None
        try:
            for raw in resp:
                line = raw.decode().rstrip("\n")
                if line.startswith("event: "):
                    event = line[7:]
                elif line.startswith("data: ") and event:
                    yield event, json.loads(line[6:])
                    event = None
        finally:
            conn.close()
