"""The golden sandbox (brief §6.2): the real §8/§9/§7 pipeline in front of a stub backend.

Wired by the composition root with in-memory stores and a sentinel secret manager, so a run
can never reach a live system and any secret that surfaces is detected.
"""

import copy
from collections import Counter
from collections.abc import Mapping, Sequence

from pydantic import SecretStr

from adapter_kernel.context import CallContext
from adapter_kernel.errors import AdapterError, ErrorCode
from adapter_kernel.jsontypes import JsonObject
from adapter_kernel.meta import ResponseMeta
from adapter_kernel.pipeline import (
    DeliveryStatus,
    Next,
    ToolFailure,
    ToolRequest,
    ToolResult,
    ToolSuccess,
)
from adapter_verify.access.domain.policy import ToolCatalog
from adapter_verify.access.ports import SecretUnavailableError
from adapter_verify.access.service import Authenticator, CredentialScoper
from adapter_verify.golden.domain.assertions import CallRecord, SandboxState, json_equal
from adapter_verify.golden.domain.definitions import ToolDefinition, argument_error
from adapter_verify.golden.domain.tasks import FixtureRef
from adapter_verify.golden.ports import ToolCall, ToolView
from adapter_verify.observability.service import (
    META_CORRELATION_ID,
    META_IDEMPOTENCY_KEY,
    META_TOOL_VERSION,
    InboundCall,
    ObservedEntry,
)


def _failure(code: ErrorCode, ctx: CallContext, delivery: DeliveryStatus | None) -> ToolResult:
    return ToolResult(
        outcome=ToolFailure(
            error=AdapterError(code=code), meta=ResponseMeta(correlation_id=ctx.correlation_id)
        ),
        delivery=delivery,
    )


class FixtureBackend:
    """Stub backend (pipeline terminal): serves fixtures, counts calls and acknowledged writes.

    It obtains the tool's scoped credential first, as a real connector would (§9.7).
    """

    def __init__(
        self,
        fixtures: Sequence[tuple[FixtureRef, JsonObject]],
        credentials: CredentialScoper | None,
    ) -> None:
        self._fixtures = tuple(fixtures)
        self._credentials = credentials
        self._calls: Counter[str] = Counter()
        self._writes = 0

    async def __call__(self, ctx: CallContext, request: ToolRequest) -> ToolResult:
        self._calls[ctx.tool] += 1
        if self._credentials is not None:
            try:
                await self._credentials.credential_for(ctx)
            except SecretUnavailableError:
                return _failure(ErrorCode.UPSTREAM_UNAVAILABLE, ctx, DeliveryStatus.NOT_SENT)
        body = next(
            (
                payload
                for ref, payload in self._fixtures
                if ref.tool == ctx.tool and _selects(ref.when, request.arguments)
            ),
            None,
        )
        if body is None:
            return _failure(ErrorCode.UPSTREAM_UNAVAILABLE, ctx, DeliveryStatus.NOT_SENT)
        if ctx.behavior.changes_state:
            self._writes += 1
        return ToolResult(
            outcome=ToolSuccess(
                content=copy.deepcopy(body), meta=ResponseMeta(correlation_id=ctx.correlation_id)
            ),
            delivery=DeliveryStatus.ACKED,
        )

    def state(self) -> SandboxState:
        return SandboxState(writes_performed=self._writes, calls_by_tool=dict(self._calls))


def _selects(when: JsonObject | None, arguments: JsonObject) -> bool:
    return when is None or all(
        k in arguments and json_equal(v, arguments[k]) for k, v in when.items()
    )


class DefinitionInputStage:
    """Stands in for the §1.3 input-validation stage, which doesn't exist yet (Romik).

    Checks arguments against the tool definition the agent was shown.
    """

    name = "input.validate"

    def __init__(self, definitions: Mapping[str, ToolDefinition]) -> None:
        self._definitions = definitions

    async def __call__(self, ctx: CallContext, request: ToolRequest, call_next: Next) -> ToolResult:
        definition = self._definitions.get(f"{ctx.tool}@{ctx.semantic_version}")
        if definition is None:
            return _failure(ErrorCode.INTERNAL, ctx, None)  # lint guarantees one exists
        if argument_error(definition, request.arguments) is not None:
            return _failure(ErrorCode.INVALID_INPUT, ctx, None)
        return await call_next(ctx, request)


class GoldenSandbox:
    """One run's session: tools/list and tools/call through the real enforcement points."""

    def __init__(  # noqa: PLR0913 - one parameter per collaborator
        self,
        *,
        entry: ObservedEntry,
        authenticator: Authenticator,
        credential: SecretStr,
        catalog: ToolCatalog,
        definitions: Mapping[str, ToolDefinition],
        backend: FixtureBackend,
        sinks: Sequence[object],
        label: str,
    ) -> None:
        self._entry = entry
        self._authenticator = authenticator
        self._credential = credential
        self._catalog = catalog
        self._definitions = definitions
        self._backend = backend
        self._sinks = tuple(sinks)
        self._label = label
        self._count = 0

    async def list_tools(self) -> list[ToolView]:
        entries = await self._authenticator.list_tools(self._credential) or []
        views = []
        for e in entries:
            d = self._definitions.get(f"{e.tool}@{e.version}")
            if d is not None:
                views.append(
                    ToolView(
                        name=d.tool,
                        version=d.version,
                        description=d.description,
                        input_schema=d.input_schema(),
                    )
                )
        return views

    async def call_tool(self, call: ToolCall) -> tuple[CallRecord, ToolSuccess | ToolFailure]:
        self._count += 1
        meta = {META_CORRELATION_ID: f"{self._label}:{self._count}"}
        if call.version is not None:
            meta[META_TOOL_VERSION] = call.version
        if call.idempotency_key is not None:
            meta[META_IDEMPOTENCY_KEY] = call.idempotency_key
        result = await self._entry.handle(
            InboundCall(
                tool=call.tool, arguments=call.arguments, meta=meta, credential=self._credential
            )
        )
        outcome = result.outcome  # delivery status stays internal (§3.4)
        entry = self._catalog.resolve(call.tool, call.version)
        record = CallRecord(
            tool=call.tool,
            version=None if entry is None else entry.version,
            arguments=call.arguments,
            error_code=outcome.error.code if isinstance(outcome, ToolFailure) else None,
        )
        return record, outcome

    def state(self) -> SandboxState:
        return self._backend.state()

    def observed(self) -> str:
        return "\n".join(repr(vars(sink)) for sink in self._sinks)
