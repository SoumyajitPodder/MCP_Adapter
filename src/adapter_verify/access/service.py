"""Access-control orchestration (brief §9): policy loading, authentication, both enforcement
points, auditing, and credential scoping."""

from collections.abc import Callable, Sequence
from dataclasses import dataclass

from pydantic import SecretStr

from adapter_kernel.context import CallContext, RequestContext
from adapter_kernel.errors import AdapterError, ErrorCode
from adapter_kernel.meta import ResponseMeta
from adapter_kernel.pipeline import Next, ToolFailure, ToolRequest, ToolResult
from adapter_verify.access.domain.credentials import CredentialMap
from adapter_verify.access.domain.evaluate import (
    RULE_TOKEN_INVALID,
    RULE_TOKEN_MISSING,
    RULE_UNKNOWN_TOOL,
    Decision,
    deny,
    evaluate,
    visible_tools,
)
from adapter_verify.access.domain.lint import LintFinding, PolicyDocument, lint
from adapter_verify.access.domain.policy import CatalogEntry, PolicySet, ToolCatalog
from adapter_verify.access.domain.semver import Version
from adapter_verify.access.ports import (
    SecretManager,
    SecretUnavailableError,
    TokenRejectedError,
    TokenVerifier,
)
from adapter_verify.common.ports import Clock
from adapter_verify.observability.audit_service import AuditTrail
from adapter_verify.observability.domain.attributes import Outcome, SpanAttributes, SpanName
from adapter_verify.observability.domain.audit import AuditKind, PrincipalKind
from adapter_verify.observability.domain.events import CallEvent
from adapter_verify.observability.ports import (
    AuditUnavailableError,
    Diagnostics,
    EventSink,
    SpanHandle,
    Telemetry,
)

UNAUTHENTICATED_PRINCIPAL = "unauthenticated"
DIAGNOSTIC_ID = "access"


class PolicyLoadError(Exception):
    def __init__(self, findings: Sequence[LintFinding]) -> None:
        super().__init__(f"{len(findings)} policy lint finding(s)")
        self.findings = tuple(findings)


class PolicyRegistry:
    """Holds the live policy set. Refuses to start on bad policy; keeps the last good on reload."""

    def __init__(
        self,
        source: Callable[[], Sequence[PolicyDocument]],
        catalog: ToolCatalog,
        diagnostics: Diagnostics,
    ) -> None:
        self._source = source
        self._catalog = catalog
        self._diagnostics = diagnostics
        self._current = self._load()

    @property
    def current(self) -> PolicySet:
        return self._current

    def reload(self) -> bool:
        try:
            candidate = self._load()
        except PolicyLoadError as exc:
            rules = ",".join(sorted({f.rule_id for f in exc.findings}))
            self._diagnostics.internal_error(DIAGNOSTIC_ID, f"policy reload rejected: {rules}")
            return False
        self._current = candidate  # single reference swap: readers never see a partial set
        return True

    def _load(self) -> PolicySet:
        documents = self._source()
        findings = lint(documents, self._catalog)
        if findings:
            raise PolicyLoadError(findings)
        return PolicySet(d.policy for d in documents if d.policy is not None)


@dataclass(frozen=True)
class AccessObserver:
    """Where access decisions are reported: span attributes, a call event, and the audit trail."""

    telemetry: Telemetry
    events: EventSink
    audit: AuditTrail
    diagnostics: Diagnostics
    clock: Clock

    async def record(  # noqa: PLR0913 - keyword-only decision context
        self,
        span: SpanHandle,
        request: RequestContext,
        decision: Decision,
        *,
        stage: SpanName,
        agent_id: str | None,
        call: CallContext | None,
    ) -> bool:
        """Report a decision. Returns False if an audit write that must succeed failed."""
        outcome = Outcome.OK if decision.allowed else Outcome.DENIED
        span.update(SpanAttributes(outcome=outcome, rule_id=decision.rule_id, agent_id=agent_id))
        if decision.error_code is not None:
            span.fail(decision.error_code)
        self.events.emit(
            CallEvent(
                timestamp=self.clock.now(),
                trace_id=span.trace_id,
                span_id=span.span_id,
                correlation_id=request.correlation_id,
                agent_id=agent_id,
                tool=request.tool,
                semantic_version=None if call is None else call.semantic_version,
                stage=stage,
                behavior=None if call is None else call.behavior,
                outcome=outcome,
                error_code=decision.error_code,
            )
        )
        state_changing = call is not None and call.behavior.changes_state
        if decision.allowed and not state_changing:
            return True  # read allows go to events only (D-037, M2-Q3)
        try:
            await self.audit.record(
                principal_kind=PrincipalKind.AGENT if agent_id else PrincipalKind.SYSTEM,
                principal=agent_id or UNAUTHENTICATED_PRINCIPAL,
                kind=AuditKind.ACCESS_DECISION,
                correlation_id=request.correlation_id,
                detail={
                    "allowed": decision.allowed,
                    "rule_id": decision.rule_id,
                    "tool": request.tool,
                    "version": None if call is None else call.semantic_version,
                    "policy_digest": decision.policy_digest,
                },
            )
        except AuditUnavailableError:
            self.diagnostics.internal_error(request.correlation_id, "access audit unavailable")
            return not decision.allowed  # a denial stays denied; an allow fails closed (M1-Q2)
        return True


def failure(code: ErrorCode, correlation_id: str) -> ToolResult:
    return ToolResult(
        outcome=ToolFailure(
            error=AdapterError(code=code), meta=ResponseMeta(correlation_id=correlation_id)
        )
    )


class Authenticator:
    """Turns a RequestContext plus bearer credential into a CallContext (R-008), or a denial."""

    def __init__(
        self,
        verifier: TokenVerifier,
        catalog: ToolCatalog,
        policies: PolicyRegistry,
        observer: AccessObserver,
    ) -> None:
        self._verifier = verifier
        self._catalog = catalog
        self._policies = policies
        self._observer = observer

    async def authenticate(
        self, request: RequestContext, credential: SecretStr | None
    ) -> CallContext | ToolResult:
        digest = self._policies.current.digest
        with self._observer.telemetry.span(
            SpanName.ACCESS_AUTHENTICATE, SpanAttributes(tool=request.tool)
        ) as span:
            if credential is None:
                decision = deny(
                    RULE_TOKEN_MISSING, ErrorCode.NOT_AUTHORIZED, "no credential", digest
                )
                return await self._denied(span, request, decision, None)
            try:
                identity = await self._verifier.verify(credential)
            except TokenRejectedError:
                decision = deny(
                    RULE_TOKEN_INVALID, ErrorCode.NOT_AUTHORIZED, "token rejected", digest
                )
                return await self._denied(span, request, decision, None)
            entry = self._catalog.resolve(request.tool, request.requested_version)
            if entry is None:
                # Same agent-facing answer as "not granted": tool names are not enumerable.
                decision = deny(RULE_UNKNOWN_TOOL, ErrorCode.NOT_AUTHORIZED, "unknown tool", digest)
                return await self._denied(span, request, decision, identity.agent_id)
            return CallContext(
                request=request,
                agent_id=identity.agent_id,
                semantic_version=entry.version,
                behavior=entry.behavior,
            )

    async def list_tools(self, credential: SecretStr | None) -> list[CatalogEntry] | None:
        """``tools/list`` enforcement point. None means unauthenticated: show nothing."""
        if credential is None:
            return None
        try:
            identity = await self._verifier.verify(credential)
        except TokenRejectedError:
            return None
        return visible_tools(self._policies.current, self._catalog, identity.agent_id)

    async def _denied(
        self, span: SpanHandle, request: RequestContext, decision: Decision, agent_id: str | None
    ) -> ToolResult:
        await self._observer.record(
            span,
            request,
            decision,
            stage=SpanName.ACCESS_AUTHENTICATE,
            agent_id=agent_id,
            call=None,
        )
        return failure(decision.error_code or ErrorCode.NOT_AUTHORIZED, request.correlation_id)


class AccessStage:
    """``tools/call`` enforcement point: the authoritative check on every call (§9.5)."""

    name = "access.check"

    def __init__(self, policies: PolicyRegistry, observer: AccessObserver) -> None:
        self._policies = policies
        self._observer = observer

    async def __call__(self, ctx: CallContext, request: ToolRequest, call_next: Next) -> ToolResult:
        with self._observer.telemetry.span(
            SpanName.ACCESS_CHECK,
            SpanAttributes(
                agent_id=ctx.agent_id, tool=ctx.tool, semantic_version=ctx.semantic_version
            ),
        ) as span:
            decision = evaluate(
                self._policies.current,
                ctx.agent_id,
                ctx.tool,
                Version.parse(ctx.semantic_version),
                ctx.behavior,
            )
            recorded = await self._observer.record(
                span,
                ctx.request,
                decision,
                stage=SpanName.ACCESS_CHECK,
                agent_id=ctx.agent_id,
                call=ctx,
            )
        if decision.error_code is not None:
            return failure(decision.error_code, ctx.correlation_id)
        if not recorded:
            return failure(ErrorCode.INTERNAL, ctx.correlation_id)
        return await call_next(ctx, request)


class CredentialScoper:
    """Per-tool credentials (§9.7): a billing tool's call cannot obtain provisioning secrets."""

    def __init__(
        self, names: CredentialMap, secrets: SecretManager, clock: Clock, *, ttl_s: float
    ) -> None:
        self._names = names
        self._secrets = secrets
        self._clock = clock
        self._ttl_s = ttl_s
        self._cache: dict[str, tuple[float, SecretStr]] = {}

    async def credential_for(self, ctx: CallContext) -> SecretStr:
        name = self._names.secret_for(ctx.tool, Version.parse(ctx.semantic_version))
        if name is None:
            raise SecretUnavailableError
        now = self._clock.monotonic()
        cached = self._cache.get(name)
        if cached is not None and now - cached[0] < self._ttl_s:
            return cached[1]
        secret = await self._secrets.get(name)
        self._cache[name] = (now, secret)
        return secret
