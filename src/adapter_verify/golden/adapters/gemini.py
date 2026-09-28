"""Gemini adapters for golden runs (DESIGN.md D-083): the reference agent and the judge.

Temperature stays at the model default: Google recommends 1.0 for Gemini 3 and warns that
lower values can cause looping (checked 2026-09-26). Repeats and thresholds absorb the variance.
The model's own turns are appended to the history unchanged, so thought signatures survive.
"""

import hashlib
import json
from collections.abc import Sequence
from typing import Any, Final, Protocol

import rfc8785
from google import genai
from google.genai import errors as genai_errors
from google.genai import types

from adapter_kernel.errors import ERROR_SPECS
from adapter_kernel.jsontypes import JsonObject
from adapter_kernel.pipeline import ToolFailure, ToolSuccess
from adapter_verify.golden.domain.judge import AnswerEvidence, JudgeVerdict
from adapter_verify.golden.domain.results import TokenUsage
from adapter_verify.golden.domain.tasks import RunLimits
from adapter_verify.golden.ports import (
    AgentRun,
    HarnessFailedError,
    JudgeUnavailableError,
    ModelUnavailableError,
    ToolCall,
    ToolCaller,
    ToolView,
)

REFERENCE_HARNESS: Final = "reference"
GEMINI_HOST: Final = "generativelanguage.googleapis.com"

_VERDICT_SCHEMA: Final[dict[str, Any]] = {
    "type": "object",
    "properties": {
        "score": {"type": "number", "minimum": 0, "maximum": 1},
        "passed": {"type": "boolean"},
        "reasons": {"type": "array", "items": {"type": "string"}, "minItems": 1},
    },
    "required": ["score", "passed", "reasons"],
}
_JUDGE_INSTRUCTIONS: Final = (
    "You grade one answer given by a customer-support agent. You receive the customer's "
    "question, the tool results the agent saw, a rubric, and the agent's final answer. "
    "Decide whether the answer meets every point of the rubric and is supported by the tool "
    "results. Return JSON only: score from 0 (fails) to 1 (fully meets), passed true exactly "
    "when score >= 0.5, and one short reason per point that decided it."
)


class AsyncModels(Protocol):
    """The part of ``genai.Client().aio.models`` the adapters use."""

    async def generate_content(
        self,
        *,
        model: str,
        contents: list[types.Content] | str,
        config: types.GenerateContentConfig | None = None,
    ) -> types.GenerateContentResponse: ...


class ClientModels:
    """``client.aio.models`` bound to its client: a collected ``Client`` closes its HTTP pool."""

    def __init__(self, client: genai.Client) -> None:
        self._client = client

    async def generate_content(
        self,
        *,
        model: str,
        contents: list[types.Content] | str,
        config: types.GenerateContentConfig | None = None,
    ) -> types.GenerateContentResponse:
        return await self._client.aio.models.generate_content(
            model=model, contents=contents, config=config
        )


_TOO_MANY_REQUESTS: Final = 429
_SERVER_ERROR: Final = 500


def _outage(exc: genai_errors.APIError) -> ModelUnavailableError | None:
    """Quota and server errors are outages (D-094); the reason is code and status only, since
    the message is free text and can echo request data (D-090, D-093)."""
    if exc.code == _TOO_MANY_REQUESTS or exc.code >= _SERVER_ERROR:
        return ModelUnavailableError(_reason(exc))
    return None


def _reason(exc: genai_errors.APIError) -> str:
    return f"{exc.code} {exc.status}"


def _usage(response: types.GenerateContentResponse) -> TokenUsage:
    meta = response.usage_metadata
    if meta is None:
        return TokenUsage()
    output = (meta.candidates_token_count or 0) + (meta.thoughts_token_count or 0)
    return TokenUsage(input_tokens=meta.prompt_token_count or 0, output_tokens=output)


def _payload(outcome: ToolSuccess | ToolFailure) -> dict[str, Any]:
    """What the model sees as the function result: the agent-facing view only."""
    if isinstance(outcome, ToolSuccess):
        return {"result": outcome.content}
    error: dict[str, Any] = {
        "code": outcome.error.code.value,
        "message": ERROR_SPECS[outcome.error.code].agent_message,
    }
    if outcome.error.retry_after_ms is not None:
        error["retry_after_ms"] = outcome.error.retry_after_ms
    return {"error": error}


def idempotency_key(tool: str, arguments: JsonObject) -> str:
    """The key an orchestrator would send: stable for the same call, so a retry can't double
    an effect (§7). Read-only tools ignore it."""
    try:
        canonical = rfc8785.dumps({"tool": tool, "arguments": arguments})
    except rfc8785.CanonicalizationError:
        canonical = repr((tool, sorted(arguments))).encode()
    return f"ref-{hashlib.sha256(canonical).hexdigest()[:40]}"


class GeminiAgent:
    """The reference agent: a plain tool-use loop over the tools ``tools/list`` returned."""

    kind = REFERENCE_HARNESS

    def __init__(self, models: AsyncModels, *, model: str, system_prompt: str) -> None:
        self._models = models
        self._model = model
        self._system_prompt = system_prompt

    async def run(
        self,
        prompt: str,
        tools: Sequence[ToolView],
        call_tool: ToolCaller,
        limits: RunLimits,
    ) -> AgentRun:
        declarations = [
            types.FunctionDeclaration(
                name=t.name, description=t.description, parameters_json_schema=t.input_schema
            )
            for t in tools
        ]
        config = types.GenerateContentConfig(
            system_instruction=self._system_prompt,
            tools=[types.Tool(function_declarations=declarations)] if declarations else None,
            automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
        )
        contents: list[types.Content] = [
            types.Content(role="user", parts=[types.Part.from_text(text=prompt)])
        ]
        usage, reported = TokenUsage(), None
        for _ in range(limits.max_tool_calls + 1):
            try:
                response = await self._models.generate_content(
                    model=self._model, contents=contents, config=config
                )
            except genai_errors.APIError as exc:
                raise _outage(exc) or HarnessFailedError(_reason(exc)) from exc
            usage += _usage(response)
            reported = response.model_version or reported
            calls = response.function_calls or []
            if not calls:
                return AgentRun(answer=response.text, usage=usage, model=reported or self._model)
            if response.candidates and response.candidates[0].content is not None:
                contents.append(response.candidates[0].content)  # keeps thought signatures
            parts = []
            for fc in calls:
                arguments: JsonObject = dict(fc.args or {})
                name = fc.name or ""
                outcome = await call_tool(
                    ToolCall(
                        tool=name,
                        arguments=arguments,
                        idempotency_key=idempotency_key(name, arguments),
                    )
                )
                parts.append(
                    types.Part(
                        function_response=types.FunctionResponse(
                            id=fc.id, name=name, response=_payload(outcome)
                        )
                    )
                )
            contents.append(types.Content(role="user", parts=parts))
        return AgentRun(answer=None, usage=usage, model=reported or self._model)


class GeminiJudge:
    """Grades an answer with schema-constrained JSON; the runner validates it again."""

    def __init__(self, models: AsyncModels, *, model: str) -> None:
        self._models = models
        self._model = model

    @property
    def model(self) -> str:
        return self._model

    async def grade(self, rubric: str, evidence: AnswerEvidence) -> JudgeVerdict:
        request = json.dumps(
            {
                "question": evidence.prompt,
                "tool_results": list(evidence.tool_results),
                "rubric": rubric,
                "answer": evidence.answer,
            },
            indent=2,
            sort_keys=True,
        )
        try:
            response = await self._models.generate_content(
                model=self._model,
                contents=request,
                config=types.GenerateContentConfig(
                    system_instruction=_JUDGE_INSTRUCTIONS,
                    response_mime_type="application/json",
                    response_json_schema=_VERDICT_SCHEMA,
                ),
            )
        except genai_errors.APIError as exc:
            raise _outage(exc) or JudgeUnavailableError(_reason(exc)) from exc
        return JudgeVerdict.model_validate_json(response.text or "")
