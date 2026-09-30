"""NVIDIA API catalog adapters for golden runs (DESIGN.md D-095): the reference agent and judge.

The API follows OpenAI chat completions (``/v1/chat/completions``), called with httpx directly.
Sampling stays at the model's default, as for Gemini (D-085). Tool names are sent with dots
replaced, since OpenAI-style function names allow only ``[A-Za-z0-9_-]``.
"""

import asyncio
import json
import re
from collections.abc import Awaitable, Callable, Sequence
from typing import Any, Final, Protocol

import httpx
from pydantic import SecretStr

from adapter_kernel.errors import ERROR_SPECS, ErrorCode
from adapter_kernel.jsontypes import JsonObject
from adapter_verify.golden.adapters.egress import EgressBlockedError
from adapter_verify.golden.adapters.llm import (
    JUDGE_INSTRUCTIONS,
    REFERENCE_HARNESS,
    VERDICT_SCHEMA,
    idempotency_key,
    judge_request,
    tool_payload,
)
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

NVIDIA_BASE_URL: Final = "https://integrate.api.nvidia.com/v1"

_RETRY_STATUSES: Final = frozenset({429, 500, 502, 503, 504})
_SERVER_ERROR: Final = 500
_TOO_MANY_REQUESTS: Final = 429
_THINKING = re.compile(r"<think>.*?</think>", re.DOTALL)
_JUDGE_SYSTEM: Final = (
    f"{JUDGE_INSTRUCTIONS} Reply with one JSON object matching this JSON Schema, and nothing "
    f"else: {json.dumps(VERDICT_SCHEMA, sort_keys=True)}"
)


class ChatApiError(Exception):
    """The API refused a request. ``str()`` is the HTTP code and reason phrase only: the body is
    free text and can echo request data (D-093)."""

    def __init__(self, code: int) -> None:
        super().__init__(f"{code} {httpx.codes.get_reason_phrase(code) or 'Unknown'}")
        self.code = code

    @property
    def outage(self) -> bool:
        """Quota and server errors (D-094)."""
        return self.code == _TOO_MANY_REQUESTS or self.code >= _SERVER_ERROR


class ChatTransportError(Exception):
    """No usable HTTP response (connection, timeout, a body that isn't a JSON object).
    ``str()`` is the exception type or a fixed reason only."""


class ChatCompletions(Protocol):
    async def create(self, body: dict[str, Any]) -> dict[str, Any]:
        """One chat completion. Raises ChatApiError or ChatTransportError."""
        ...


class HttpxChatCompletions:
    """``POST {base_url}/chat/completions``, retrying quota and server errors with backoff.

    Retries stop early, raising the last error, when the next wait would exceed ``max_delay_s``
    (a longer ``Retry-After`` would only be refused again) or take the total wait past
    ``retry_budget_s``. The budget keeps retries inside a run's timeout, so a sustained outage
    ends as ``model_unavailable`` instead of a run timeout (D-094). A connection the egress guard
    refused is not retried.

    A fresh connection pool per request: runs are sequential and few, and nothing can close a
    shared pool under a later call (the Gemini lesson, Session 19).
    """

    def __init__(  # noqa: PLR0913 - retry policy is configuration, not state
        self,
        api_key: SecretStr,
        *,
        base_url: str = NVIDIA_BASE_URL,
        timeout_s: float = 90.0,
        attempts: int = 5,
        initial_delay_s: float = 2.0,
        max_delay_s: float = 60.0,
        retry_budget_s: float = 30.0,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._key = api_key
        self._url = base_url.rstrip("/") + "/chat/completions"
        self._timeout = timeout_s
        self._attempts = attempts
        self._delay = initial_delay_s
        self._max_delay = max_delay_s
        self._budget = retry_budget_s
        self._sleep = sleep
        self._transport = transport

    async def create(self, body: dict[str, Any]) -> dict[str, Any]:
        delay = self._delay
        waited = 0.0
        for attempt in range(1, self._attempts + 1):
            try:
                async with httpx.AsyncClient(
                    timeout=self._timeout, transport=self._transport
                ) as client:
                    response = await client.post(
                        self._url,
                        json=body,
                        headers={
                            "Authorization": f"Bearer {self._key.get_secret_value()}",
                            "Accept": "application/json",
                        },
                    )
            except httpx.TransportError as exc:
                error: Exception = ChatTransportError(type(exc).__name__)
                wait = None if _egress_blocked(exc) else delay
            else:
                if response.status_code == httpx.codes.OK:
                    return _json_body(response)
                error = ChatApiError(response.status_code)
                retryable = response.status_code in _RETRY_STATUSES
                after = _retry_after(response)
                wait = (delay if after is None else after) if retryable else None
            if (
                wait is None
                or attempt == self._attempts
                or wait > self._max_delay
                or waited + wait > self._budget
            ):
                raise error from None
            await self._sleep(wait)
            waited += wait
            delay = min(delay * 2, self._max_delay)
        raise AssertionError  # pragma: no cover - the loop always returns or raises


def _egress_blocked(exc: BaseException) -> bool:
    seen: BaseException | None = exc
    while seen is not None:
        if isinstance(seen, EgressBlockedError):
            return True
        seen = seen.__cause__ or seen.__context__
    return False


def _json_body(response: httpx.Response) -> dict[str, Any]:
    try:
        data = response.json()
    except ValueError:
        raise ChatTransportError("response body is not JSON") from None
    if not isinstance(data, dict):
        raise ChatTransportError("response body is not a JSON object")
    return data


def _retry_after(response: httpx.Response) -> float | None:
    """Seconds from ``Retry-After``; None when absent or an HTTP date (then backoff applies)."""
    value = response.headers.get("retry-after", "").strip()
    return float(value) if value.isascii() and value.isdigit() else None


def _usage(data: dict[str, Any]) -> TokenUsage:
    usage = data.get("usage") or {}
    return TokenUsage(
        input_tokens=int(usage.get("prompt_tokens") or 0),
        output_tokens=int(usage.get("completion_tokens") or 0),
    )


def _message(data: dict[str, Any]) -> dict[str, Any]:
    choices = data.get("choices") or []
    message = choices[0].get("message") if choices and isinstance(choices[0], dict) else None
    if not isinstance(message, dict):
        msg = "response has no message"
        raise HarnessFailedError(msg)
    return message


def _visible(text: str | None) -> str | None:
    """The answer without inline reasoning (some models emit ``<think>`` blocks in content)."""
    if text is None:
        return None
    return _THINKING.sub("", text).strip() or None


def wire_name(tool: str) -> str:
    return tool.replace(".", "_")


async def _call(models: ChatCompletions, body: dict[str, Any], *, judge: bool) -> dict[str, Any]:
    try:
        return await models.create(body)
    except ChatApiError as exc:
        if exc.outage:
            raise ModelUnavailableError(str(exc)) from None
        if judge:
            raise JudgeUnavailableError(str(exc)) from None
        raise HarnessFailedError(str(exc)) from None
    except ChatTransportError as exc:
        raise ModelUnavailableError(f"no response: {exc}") from None


class NvidiaAgent:
    """The reference agent on an OpenAI-style tool-use loop."""

    kind = REFERENCE_HARNESS

    def __init__(self, models: ChatCompletions, *, model: str, system_prompt: str) -> None:
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
        names = {wire_name(t.name): t.name for t in tools}
        if len(names) != len(tools):
            msg = "two tools share a wire name"
            raise HarnessFailedError(msg)
        body: dict[str, Any] = {"model": self._model}
        if tools:
            body["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": wire_name(t.name),
                        "description": t.description,
                        "parameters": t.input_schema,
                    },
                }
                for t in tools
            ]
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": self._system_prompt},
            {"role": "user", "content": prompt},
        ]
        usage, reported = TokenUsage(), None
        for _ in range(limits.max_tool_calls + 1):
            data = await _call(self._models, {**body, "messages": messages}, judge=False)
            usage += _usage(data)
            reported = data.get("model") or reported
            message = _message(data)
            calls = message.get("tool_calls") or []
            if not calls:
                return AgentRun(
                    answer=_visible(message.get("content")),
                    usage=usage,
                    model=reported or self._model,
                )
            messages.append(
                {"role": "assistant", "content": message.get("content"), "tool_calls": calls}
            )
            for tc in calls:  # in order: a later call may depend on an earlier effect
                result = await self._invoke(tc, names, call_tool)
                messages.append(
                    {"role": "tool", "tool_call_id": tc.get("id"), "content": json.dumps(result)}
                )
        return AgentRun(answer=None, usage=usage, model=reported or self._model)

    @staticmethod
    async def _invoke(
        tc: dict[str, Any], names: dict[str, str], call_tool: ToolCaller
    ) -> dict[str, Any]:
        """Runs one requested call through the sandbox. Calls it can't decode are answered
        with an error and never reach the sandbox, like a malformed MCP request."""
        function = tc.get("function") or {}
        wire = str(function.get("name") or "")
        try:
            arguments = json.loads(function.get("arguments") or "{}")
        except json.JSONDecodeError:
            arguments = None
        if not isinstance(arguments, dict):
            code = ErrorCode.INVALID_INPUT
            return {"error": {"code": code.value, "message": ERROR_SPECS[code].agent_message}}
        tool = names.get(wire, wire)
        args: JsonObject = arguments
        outcome = await call_tool(
            ToolCall(tool=tool, arguments=args, idempotency_key=idempotency_key(tool, args))
        )
        return tool_payload(outcome)


class NvidiaJudge:
    """Grades an answer; the schema is in the prompt and the reply is validated strictly."""

    def __init__(self, models: ChatCompletions, *, model: str) -> None:
        self._models = models
        self._model = model

    @property
    def model(self) -> str:
        return self._model

    async def grade(self, rubric: str, evidence: AnswerEvidence) -> JudgeVerdict:
        data = await _call(
            self._models,
            {
                "model": self._model,
                "messages": [
                    {"role": "system", "content": _JUDGE_SYSTEM},
                    {"role": "user", "content": judge_request(rubric, evidence)},
                ],
            },
            judge=True,
        )
        try:
            text = _visible(_message(data).get("content")) or ""
        except HarnessFailedError as exc:
            raise JudgeUnavailableError(str(exc)) from None
        return JudgeVerdict.model_validate_json(_json_object(text))


def _json_object(text: str) -> str:
    """The outermost ``{...}`` in the reply, so a code fence around it doesn't matter. The
    strict model validation that follows is the real check."""
    start, end = text.find("{"), text.rfind("}")
    return text[start : end + 1] if 0 <= start < end else text
