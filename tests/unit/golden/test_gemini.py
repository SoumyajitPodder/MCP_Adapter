"""Gemini adapters against a fake client that returns real SDK response objects. No network."""

import json
from collections.abc import Sequence
from typing import Any

import pytest
from google.genai import types
from pydantic import ValidationError

from adapter_kernel.errors import AdapterError, ErrorCode
from adapter_kernel.meta import ResponseMeta
from adapter_kernel.pipeline import ToolFailure, ToolSuccess
from adapter_verify.golden.adapters.gemini import GeminiAgent, GeminiJudge, idempotency_key
from adapter_verify.golden.domain.judge import AnswerEvidence
from adapter_verify.golden.domain.tasks import RunLimits
from adapter_verify.golden.ports import ToolCall, ToolView

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]

VIEW = ToolView(
    name="order.get",
    version="1.2.0",
    description="Look up one order.",
    input_schema={"type": "object", "properties": {"order_id": {"type": "string"}}},
)


def _response(
    *parts: types.Part, prompt: int = 10, output: int = 5, thoughts: int | None = 2
) -> types.GenerateContentResponse:
    return types.GenerateContentResponse(
        candidates=[types.Candidate(content=types.Content(role="model", parts=list(parts)))],
        usage_metadata=types.GenerateContentResponseUsageMetadata(
            prompt_token_count=prompt, candidates_token_count=output, thoughts_token_count=thoughts
        ),
        model_version="gemini-3.5-flash-001",
    )


def _call(name: str, call_id: str, **args: Any) -> types.Part:
    return types.Part(function_call=types.FunctionCall(id=call_id, name=name, args=args))


class FakeModels:
    def __init__(self, responses: Sequence[types.GenerateContentResponse]) -> None:
        self._responses = list(responses)
        self.requests: list[tuple[str, Any, types.GenerateContentConfig | None]] = []

    async def generate_content(
        self,
        *,
        model: str,
        contents: list[types.Content] | str,
        config: types.GenerateContentConfig | None = None,
    ) -> types.GenerateContentResponse:
        self.requests.append(
            (model, list(contents) if isinstance(contents, list) else contents, config)
        )
        return self._responses.pop(0)


class Tools:
    def __init__(self) -> None:
        self.calls: list[ToolCall] = []

    async def __call__(self, call: ToolCall) -> ToolSuccess | ToolFailure:
        self.calls.append(call)
        meta = ResponseMeta(correlation_id="c")
        if call.arguments.get("order_id") == "1":
            return ToolSuccess(content={"status": "in_progress"}, meta=meta)
        return ToolFailure(
            error=AdapterError(code=ErrorCode.DUPLICATE_IN_PROGRESS, retry_after_ms=500), meta=meta
        )


async def test_agent_loop_calls_tools_and_returns_the_answer() -> None:
    models = FakeModels(
        [
            _response(
                _call("order.get", "c1", order_id="1"), _call("order.get", "c2", order_id="2")
            ),
            _response(types.Part.from_text(text="It is in progress."), thoughts=None),
        ]
    )
    tools = Tools()
    agent = GeminiAgent(models, model="gemini-3.5-flash", system_prompt="Be helpful.")
    run = await agent.run("Is order 1 shipped?", [VIEW], tools, RunLimits())

    assert run.answer == "It is in progress."
    assert run.model == "gemini-3.5-flash-001"
    assert (run.usage.input_tokens, run.usage.output_tokens) == (20, 12)
    assert [c.arguments for c in tools.calls] == [{"order_id": "1"}, {"order_id": "2"}]
    assert tools.calls[0].idempotency_key == idempotency_key("order.get", {"order_id": "1"})

    model, first, config = models.requests[0]
    assert model == "gemini-3.5-flash"
    assert config is not None
    assert config.system_instruction == "Be helpful."
    assert config.temperature is None  # Gemini 3: keep the default (Google's guidance)
    assert config.automatic_function_calling is not None
    assert config.automatic_function_calling.disable is True
    assert config.tools is not None
    tool = config.tools[0]
    assert isinstance(tool, types.Tool)
    assert tool.function_declarations is not None
    declaration = tool.function_declarations[0]
    assert (declaration.name, declaration.parameters_json_schema) == (
        "order.get",
        VIEW.input_schema,
    )
    assert len(first) == 1

    _, second, _ = models.requests[1]
    assert second[1].role == "model"  # the model's own turn, unchanged (thought signatures)
    responses = [p.function_response for p in second[2].parts]
    assert responses[0] is not None
    assert (responses[0].id, responses[0].response) == ("c1", {"result": {"status": "in_progress"}})
    assert responses[1] is not None
    assert responses[1].response == {
        "error": {
            "code": "DUPLICATE_IN_PROGRESS",
            "message": "An identical call is still in progress. Retry after the given delay.",
            "retry_after_ms": 500,
        }
    }


async def test_agent_stops_at_the_call_limit_without_an_answer() -> None:
    looping = [_response(_call("order.get", f"c{i}", order_id="1")) for i in range(3)]
    agent = GeminiAgent(FakeModels(looping), model="m", system_prompt="s")
    run = await agent.run("q", [], Tools(), RunLimits(max_tool_calls=2))
    assert run.answer is None
    assert run.model == "gemini-3.5-flash-001"


async def test_agent_without_tools_or_usage() -> None:
    response = types.GenerateContentResponse(
        candidates=[
            types.Candidate(
                content=types.Content(role="model", parts=[types.Part.from_text(text="Hi")])
            )
        ]
    )
    models = FakeModels([response])
    run = await GeminiAgent(models, model="m", system_prompt="s").run("q", [], Tools(), RunLimits())
    assert (run.answer, run.usage.total, run.model) == ("Hi", 0, "m")
    assert models.requests[0][2] is not None
    assert models.requests[0][2].tools is None


async def test_idempotency_keys_are_stable_per_call() -> None:
    a = idempotency_key("order.cancel", {"order_id": "1", "reason": "x"})
    assert a == idempotency_key("order.cancel", {"reason": "x", "order_id": "1"})
    assert a != idempotency_key("order.cancel", {"order_id": "2", "reason": "x"})
    assert idempotency_key("t", {"x": float("nan")}).startswith("ref-")


async def test_judge_returns_a_validated_verdict() -> None:
    verdict = {"score": 0.9, "passed": True, "reasons": ["says in progress"]}
    models = FakeModels([_response(types.Part.from_text(text=json.dumps(verdict)))])
    judge = GeminiJudge(models, model="gemini-3.8-flash")
    evidence = AnswerEvidence(prompt="q", tool_results=({"status": "in_progress"},), answer="a")
    result = await judge.grade("rubric", evidence)
    assert (result.score, result.passed, result.reasons) == (0.9, True, ("says in progress",))
    assert judge.model == "gemini-3.8-flash"
    _, contents, config = models.requests[0]
    assert json.loads(contents)["rubric"] == "rubric"
    assert config is not None
    assert config.response_mime_type == "application/json"


@pytest.mark.parametrize("text", ["not json", '{"score": 2, "passed": true, "reasons": ["x"]}', ""])
async def test_judge_rejects_invalid_output(text: str) -> None:
    models = FakeModels([_response(types.Part.from_text(text=text))])
    with pytest.raises(ValidationError):
        await GeminiJudge(models, model="m").grade(
            "r", AnswerEvidence(prompt="q", tool_results=(), answer="a")
        )
