"""NVIDIA adapters against a fake chat API and httpx's mock transport. No network."""

import json
from collections.abc import Sequence
from typing import Any

import httpx
import pytest
from pydantic import SecretStr, ValidationError

from adapter_kernel.errors import AdapterError, ErrorCode
from adapter_kernel.meta import ResponseMeta
from adapter_kernel.pipeline import ToolFailure, ToolSuccess
from adapter_verify.golden.adapters.llm import idempotency_key
from adapter_verify.golden.adapters.nvidia import (
    ChatApiError,
    ChatTransportError,
    HttpxChatCompletions,
    NvidiaAgent,
    NvidiaJudge,
)
from adapter_verify.golden.domain.judge import AnswerEvidence
from adapter_verify.golden.domain.tasks import RunLimits
from adapter_verify.golden.ports import (
    HarnessFailedError,
    JudgeUnavailableError,
    ModelUnavailableError,
    ToolCall,
    ToolView,
)

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]

VIEW = ToolView(
    name="order.get",
    version="1.2.0",
    description="Look up one order.",
    input_schema={"type": "object", "properties": {"order_id": {"type": "string"}}},
)
EVIDENCE = AnswerEvidence(prompt="q", tool_results=(), answer="a")


def _reply(
    content: str | None = None, *calls: dict[str, Any], prompt: int = 10, output: int = 5
) -> dict[str, Any]:
    message: dict[str, Any] = {"role": "assistant", "content": content}
    if calls:
        message["tool_calls"] = list(calls)
    return {
        "model": "nvidia/nemotron-test",
        "choices": [{"index": 0, "message": message}],
        "usage": {"prompt_tokens": prompt, "completion_tokens": output},
    }


def _call(name: str, call_id: str, arguments: str) -> dict[str, Any]:
    return {"id": call_id, "type": "function", "function": {"name": name, "arguments": arguments}}


class FakeCompletions:
    def __init__(self, replies: Sequence[dict[str, Any] | Exception]) -> None:
        self._replies = list(replies)
        self.bodies: list[dict[str, Any]] = []

    async def create(self, body: dict[str, Any]) -> dict[str, Any]:
        self.bodies.append(json.loads(json.dumps(body)))  # a snapshot, not the live list
        reply = self._replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply


class Tools:
    def __init__(self) -> None:
        self.calls: list[ToolCall] = []

    async def __call__(self, call: ToolCall) -> ToolSuccess | ToolFailure:
        self.calls.append(call)
        meta = ResponseMeta(correlation_id="c")
        if call.arguments.get("order_id") == "1":
            return ToolSuccess(content={"status": "in_progress"}, meta=meta)
        return ToolFailure(error=AdapterError(code=ErrorCode.INVALID_INPUT), meta=meta)


async def test_agent_loop_calls_tools_and_returns_the_answer() -> None:
    models = FakeCompletions(
        [
            _reply(None, _call("order_get", "c1", '{"order_id": "1"}')),
            _reply(
                None,
                _call("order_get", "c2", '{"order_id": "2"}'),
                _call("order_get", "c3", "[1]"),
                _call("order_get", "c4", "{not json"),
            ),
            _reply("<think>checking</think>Your order is in progress."),
        ]
    )
    tools = Tools()
    agent = NvidiaAgent(models, model="m", system_prompt="be brief")
    run = await agent.run("Where is order 1?", [VIEW], tools, RunLimits())
    assert run.answer == "Your order is in progress."
    assert (run.usage.input_tokens, run.usage.output_tokens) == (30, 15)
    assert run.model == "nvidia/nemotron-test"
    assert [c.tool for c in tools.calls] == ["order.get", "order.get"]  # "[1]" never reached it
    assert tools.calls[0].idempotency_key == idempotency_key("order.get", {"order_id": "1"})

    first, last = models.bodies[0], models.bodies[-1]
    assert first["model"] == "m"
    assert first["tools"][0]["function"]["name"] == "order_get"
    assert first["messages"][0] == {"role": "system", "content": "be brief"}
    assert "temperature" not in first  # model default (D-085)
    results = [m for m in last["messages"] if m["role"] == "tool"]
    assert [m["tool_call_id"] for m in results] == ["c1", "c2", "c3", "c4"]
    assert json.loads(results[0]["content"]) == {"result": {"status": "in_progress"}}
    assert json.loads(results[1]["content"])["error"]["code"] == "INVALID_INPUT"
    assert json.loads(results[2]["content"])["error"]["code"] == "INVALID_INPUT"
    assert json.loads(results[3]["content"])["error"]["code"] == "INVALID_INPUT"


async def test_agent_stops_at_the_call_limit_without_an_answer() -> None:
    looping = [_reply(None, _call("order_get", f"c{i}", '{"order_id": "1"}')) for i in range(3)]
    run = await NvidiaAgent(FakeCompletions(looping), model="m", system_prompt="s").run(
        "q", [VIEW], Tools(), RunLimits(max_tool_calls=2)
    )
    assert run.answer is None


async def test_agent_without_tools_sends_none_and_tolerates_missing_usage() -> None:
    models = FakeCompletions([{"choices": [{"message": {"content": "Hi"}}]}])
    run = await NvidiaAgent(models, model="m", system_prompt="s").run("q", [], Tools(), RunLimits())
    assert (run.answer, run.usage.total, run.model) == ("Hi", 0, "m")
    assert "tools" not in models.bodies[0]
    silent = FakeCompletions([_reply(None)])
    run = await NvidiaAgent(silent, model="m", system_prompt="s").run("q", [], Tools(), RunLimits())
    assert run.answer is None


async def test_agent_rejects_colliding_wire_names_and_empty_replies() -> None:
    twin = VIEW.model_copy(update={"name": "order_get"})
    with pytest.raises(HarnessFailedError, match="wire name"):
        await NvidiaAgent(FakeCompletions([]), model="m", system_prompt="s").run(
            "q", [VIEW, twin], Tools(), RunLimits()
        )
    with pytest.raises(HarnessFailedError, match="no message"):
        await NvidiaAgent(FakeCompletions([{"choices": []}]), model="m", system_prompt="s").run(
            "q", [], Tools(), RunLimits()
        )


@pytest.mark.parametrize(
    ("error", "raised", "reason"),
    [
        (ChatApiError(503), ModelUnavailableError, "503 Service Unavailable"),
        (ChatApiError(429), ModelUnavailableError, "429 Too Many Requests"),
        (ChatApiError(400), HarnessFailedError, "400 Bad Request"),
        (
            ChatTransportError("ConnectTimeout"),
            ModelUnavailableError,
            "no response: ConnectTimeout",
        ),
    ],
)
async def test_agent_reports_api_errors_by_code_only(
    error: Exception, raised: type[Exception], reason: str
) -> None:
    agent = NvidiaAgent(FakeCompletions([error]), model="m", system_prompt="s")
    with pytest.raises(raised) as caught:
        await agent.run("q", [VIEW], Tools(), RunLimits())
    assert str(caught.value) == reason


@pytest.mark.parametrize(
    "text",
    [
        '{"score": 0.9, "passed": true, "reasons": ["ok"]}',
        '```json\n{"score": 0.9, "passed": true, "reasons": ["ok"]}\n```',
        '<think>{"draft": 1}</think>{"score": 0.9, "passed": true, "reasons": ["ok"]}',
    ],
)
async def test_judge_parses_the_verdict(text: str) -> None:
    models = FakeCompletions([_reply(text)])
    judge = NvidiaJudge(models, model="j")
    verdict = await judge.grade("rubric", EVIDENCE)
    assert (verdict.score, verdict.passed, verdict.reasons) == (0.9, True, ("ok",))
    assert judge.model == "j"
    system, user = models.bodies[0]["messages"]
    assert '"required": ["score", "passed", "reasons"]' in system["content"]
    assert json.loads(user["content"])["rubric"] == "rubric"


@pytest.mark.parametrize("text", ["not json", '{"score": 2, "passed": true, "reasons": ["x"]}', ""])
async def test_judge_rejects_invalid_output(text: str) -> None:
    with pytest.raises(ValidationError):
        await NvidiaJudge(FakeCompletions([_reply(text)]), model="j").grade("r", EVIDENCE)


@pytest.mark.parametrize(
    ("reply", "raised"),
    [
        (ChatApiError(429), ModelUnavailableError),
        (ChatApiError(404), JudgeUnavailableError),
        ({"choices": []}, JudgeUnavailableError),
    ],
)
async def test_judge_failures(reply: dict[str, Any] | Exception, raised: type[Exception]) -> None:
    with pytest.raises(raised):
        await NvidiaJudge(FakeCompletions([reply]), model="j").grade("r", EVIDENCE)


def _client(handler: Any, sleeps: list[float], attempts: int = 3) -> HttpxChatCompletions:
    async def sleep(seconds: float) -> None:
        sleeps.append(seconds)

    return HttpxChatCompletions(
        SecretStr("test-key-not-real"),
        base_url="https://api.example.invalid/v1/",
        attempts=attempts,
        initial_delay_s=1.0,
        max_delay_s=5.0,
        sleep=sleep,
        transport=httpx.MockTransport(handler),
    )


async def test_client_posts_with_the_key_and_retries_quota_and_server_errors() -> None:
    seen: list[httpx.Request] = []
    statuses = iter([429, 503, 200])

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        status = next(statuses)
        if status == 200:
            return httpx.Response(200, json={"choices": []})
        return httpx.Response(status, headers={"retry-after": "3"} if status == 429 else {})

    sleeps: list[float] = []
    assert await _client(handler, sleeps).create({"model": "m"}) == {"choices": []}
    assert sleeps == [3.0, 2.0]  # Retry-After first, then doubling backoff
    assert str(seen[0].url) == "https://api.example.invalid/v1/chat/completions"
    assert seen[0].headers["authorization"] == "Bearer test-key-not-real"
    assert json.loads(seen[0].content) == {"model": "m"}


async def test_client_gives_up_with_the_code_only() -> None:
    sleeps: list[float] = []
    always_busy = _client(lambda _r: httpx.Response(503, text="echoes the request"), sleeps)
    with pytest.raises(ChatApiError) as caught:
        await always_busy.create({})
    assert (str(caught.value), caught.value.outage) == ("503 Service Unavailable", True)
    assert sleeps == [1.0, 2.0]

    bad = _client(lambda _r: httpx.Response(400, text="echoes the request"), [])
    with pytest.raises(ChatApiError, match=r"^400 Bad Request$") as caught:
        await bad.create({})
    assert caught.value.outage is False

    not_object = _client(lambda _r: httpx.Response(200, json=[1]), [])
    with pytest.raises(ChatApiError, match=r"^200 OK$"):
        await not_object.create({})


async def test_client_retries_transport_errors_then_reports_the_type() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        msg = "refused"
        raise httpx.ConnectError(msg, request=request)

    sleeps: list[float] = []
    with pytest.raises(ChatTransportError, match=r"^ConnectError$"):
        await _client(handler, sleeps, attempts=2).create({})
    assert sleeps == [1.0]
