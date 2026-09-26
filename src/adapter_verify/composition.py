"""Composition root: the only module that wires ports to adapters (brief §2).

Stage order is fixed here and covered by a test (§3.5).
"""

import difflib
import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

import asyncpg
from pydantic import SecretStr

from adapter_kernel.pipeline import Next, Stage
from adapter_verify.access.adapters.files import (
    load_catalog,
    load_credential_map,
    load_policy_documents,
)
from adapter_verify.access.adapters.jwt import JwksCache, JwtTokenVerifier, https_jwks_fetcher
from adapter_verify.access.domain.lint import PolicyDocument
from adapter_verify.access.domain.policy import PolicySet, ToolCatalog
from adapter_verify.access.fakes import SentinelSecretManager, StaticTokenVerifier
from adapter_verify.access.ports import SecretManager, TokenVerifier
from adapter_verify.access.service import (
    AccessObserver,
    AccessStage,
    Authenticator,
    CredentialScoper,
    PolicyRegistry,
)
from adapter_verify.common.adapters.postgres import load_migrations, migrate
from adapter_verify.common.adapters.system import SystemClock, SystemEntropy
from adapter_verify.common.adapters.yamlfile import ConfigFileError, parse_yaml_model
from adapter_verify.common.ports import Clock, Entropy
from adapter_verify.contract_ci.adapters.files import FileContractRepository
from adapter_verify.contract_ci.domain.contracts import CanonicalContract
from adapter_verify.contract_ci.domain.extract import EvidenceThreshold
from adapter_verify.contract_ci.service import AccessView, ContractCi
from adapter_verify.golden.adapters.egress import SocketEgressGuard
from adapter_verify.golden.adapters.files import Workspace, load_calibration, load_workspace
from adapter_verify.golden.adapters.git import GitRepo
from adapter_verify.golden.domain.definitions import ToolDefinition
from adapter_verify.golden.domain.select import Layout, input_digest
from adapter_verify.golden.domain.tasks import AgentConfig, GoldenTask
from adapter_verify.golden.ports import AgentHarness, HarnessUnavailableError, SandboxSession
from adapter_verify.golden.sandbox import DefinitionInputStage, FixtureBackend, GoldenSandbox
from adapter_verify.golden.service import GoldenRunner
from adapter_verify.idempotency.adapters.alerts import LogOwnerAlerts
from adapter_verify.idempotency.adapters.postgres import PostgresIdempotencyStore
from adapter_verify.idempotency.domain.records import Timing
from adapter_verify.idempotency.fakes import MemoryIdempotencyStore, MemoryOwnerAlerts
from adapter_verify.idempotency.ports import IdempotencyStore, OwnerAlerts
from adapter_verify.idempotency.service import (
    IdempotencyAudit,
    IdempotencyMaintenance,
    IdempotencyObserver,
    IdempotencyStage,
)
from adapter_verify.observability.adapters.otel import OtelTelemetry, build_tracer_provider
from adapter_verify.observability.adapters.postgres import PostgresAuditStore, PostgresEventStore
from adapter_verify.observability.adapters.stdlog import LogDiagnostics, LogEventSink
from adapter_verify.observability.audit_service import AuditTrail
from adapter_verify.observability.buffered_sink import BufferedEventSink
from adapter_verify.observability.fakes import (
    MemoryAuditStore,
    MemoryDiagnostics,
    MemoryEventSink,
    MemoryPayloadStore,
    RecordingTelemetry,
)
from adapter_verify.observability.ports import (
    Diagnostics,
    EventSink,
    EventWriter,
    PayloadStore,
    Telemetry,
    TraceQuery,
)
from adapter_verify.observability.service import Downstream, ObservedEntry, SampledEventSink
from adapter_verify.pipeline import AuthenticatedPipeline
from adapter_verify.settings import (
    AccessSettings,
    ContractSettings,
    DatabaseSettings,
    GoldenSettings,
    IdempotencySettings,
    ObservabilitySettings,
)

MIGRATIONS_DIR = Path(__file__).resolve().parents[2] / "migrations"


@dataclass(frozen=True)
class Runtime:
    entry: ObservedEntry
    shutdown: Callable[[], None]
    event_buffer: BufferedEventSink | None
    """Run ``event_buffer.run(settings.event_flush_interval_s)`` as a task when present."""


def build_runtime(
    downstream: Downstream,
    settings: ObservabilitySettings,
    event_writer: EventWriter | None = None,
) -> Runtime:
    """Wire the entry. Without an event writer, events go to structured logs only."""
    clock, entropy = SystemClock(), SystemEntropy()
    diagnostics = LogDiagnostics(logging.getLogger("adapter_verify.diagnostics"))
    provider = build_tracer_provider(settings)
    buffer = (
        None
        if event_writer is None
        else BufferedEventSink(
            event_writer,
            diagnostics,
            capacity=settings.event_buffer_capacity,
            batch_size=settings.event_batch_size,
        )
    )
    inner: EventSink = buffer or LogEventSink(logging.getLogger("adapter_verify.events"))
    entry = ObservedEntry(
        telemetry=OtelTelemetry(provider.get_tracer("adapter_verify")),
        events=SampledEventSink(inner, read_ratio=settings.read_sample_ratio, entropy=entropy),
        diagnostics=diagnostics,
        clock=clock,
        entropy=entropy,
        downstream=downstream,
    )
    return Runtime(entry=entry, shutdown=provider.shutdown, event_buffer=buffer)


async def open_pool(settings: DatabaseSettings) -> asyncpg.Pool:
    return await asyncpg.create_pool(
        dsn=settings.dsn.get_secret_value(),
        min_size=settings.pool_min_size,
        max_size=settings.pool_max_size,
    )


async def run_migrations(pool: asyncpg.Pool, directory: Path = MIGRATIONS_DIR) -> list[str]:
    return await migrate(pool, load_migrations(directory))


@dataclass(frozen=True)
class Access:
    authenticator: Authenticator
    stage: AccessStage
    policies: PolicyRegistry
    credentials: CredentialScoper


def load_access_config(settings: AccessSettings) -> tuple[list[PolicyDocument], ToolCatalog]:
    return load_policy_documents(settings.policies_dir), load_catalog(settings.catalog_path)


def token_verifier(settings: AccessSettings, clock: Clock) -> TokenVerifier:
    if not (settings.jwt_issuer and settings.jwt_audience and settings.jwks_url):
        msg = "ADAPTER_ACCESS_JWT_ISSUER, _JWT_AUDIENCE and _JWKS_URL are all required"
        raise ValueError(msg)
    keys = JwksCache(
        https_jwks_fetcher(settings.jwks_url, timeout_s=5.0),
        clock,
        ttl_s=settings.jwks_ttl_s,
        min_refresh_s=30.0,
    )
    return JwtTokenVerifier(
        keys,
        issuer=settings.jwt_issuer,
        audience=settings.jwt_audience,
        leeway_s=settings.clock_skew_s,
    )


def build_access(  # noqa: PLR0913 - one parameter per port
    settings: AccessSettings,
    *,
    verifier: TokenVerifier,
    secrets: SecretManager,
    audit: AuditTrail,
    telemetry: Telemetry,
    events: EventSink,
    diagnostics: Diagnostics,
    clock: Clock,
) -> Access:
    """Fails fast: an invalid policy set, catalog or credential map stops startup (§9.8)."""
    catalog = load_catalog(settings.catalog_path)
    registry = PolicyRegistry(
        lambda: load_policy_documents(settings.policies_dir), catalog, diagnostics
    )
    observer = AccessObserver(telemetry, events, audit, diagnostics, clock)
    return Access(
        authenticator=Authenticator(verifier, catalog, registry, observer),
        stage=AccessStage(registry, observer),
        policies=registry,
        credentials=CredentialScoper(
            load_credential_map(settings.credentials_path),
            secrets,
            clock,
            ttl_s=settings.secret_cache_ttl_s,
        ),
    )


def timing(settings: IdempotencySettings) -> Timing:
    return Timing(
        lease_s=settings.lease_s,
        retention_h=settings.retention_h,
        lease_overrides_s=settings.lease_overrides_s,
        retention_overrides_h=settings.retention_overrides_h,
    )


def build_idempotency(  # noqa: PLR0913 - one parameter per port
    settings: IdempotencySettings,
    *,
    store: IdempotencyStore,
    payloads: PayloadStore,
    audit: AuditTrail,
    telemetry: Telemetry,
    events: EventSink,
    diagnostics: Diagnostics,
    alerts: OwnerAlerts,
    clock: Clock,
    entropy: Entropy,
) -> IdempotencyStage:
    """TODO(owner): check at startup that every lease exceeds its connector timeout, once
    connector configuration exists (Romik)."""
    observer = IdempotencyObserver(
        telemetry, events, IdempotencyAudit(audit, diagnostics), alerts, clock
    )
    return IdempotencyStage(store, payloads, observer, timing(settings), entropy)


def stage_order(access: Access, idempotency: IdempotencyStage) -> list[Stage]:
    """The fixed stage order (§1.3, §3.5). §4 lifecycle and input validation join between
    access and idempotency; the §3/§2 stages join after it."""
    return [access.stage, idempotency]


def build_pipeline(
    access: Access, idempotency: IdempotencyStage, terminal: Next
) -> AuthenticatedPipeline:
    return AuthenticatedPipeline(access.authenticator, stage_order(access, idempotency), terminal)


def contract_ci(settings: ContractSettings) -> ContractCi:
    root = settings.root
    repo = FileContractRepository(
        root=root,
        sources_dir=root / settings.sources_dir,
        baselines_dir=root / settings.baselines_dir,
        contracts_dir=root / settings.contracts_dir,
        mappings_dir=root / settings.mappings_dir,
    )
    return ContractCi(
        repo,
        SystemClock(),
        threshold=EvidenceThreshold(
            min_samples=settings.inferred_min_samples, min_days=settings.inferred_min_days
        ),
        rename_threshold=settings.rename_threshold,
    )


def canonical_contracts(settings: ContractSettings) -> Sequence[CanonicalContract]:
    root = settings.root
    return FileContractRepository(
        root=root,
        sources_dir=root / settings.sources_dir,
        baselines_dir=root / settings.baselines_dir,
        contracts_dir=root / settings.contracts_dir,
        mappings_dir=root / settings.mappings_dir,
    ).contracts()


def access_view(settings: AccessSettings) -> AccessView:
    """Access config as contract CI sees it. Lint-invalid policies are left out of the join."""
    documents = load_policy_documents(settings.policies_dir)
    return AccessView(
        catalog=load_catalog(settings.catalog_path),
        credentials=load_credential_map(settings.credentials_path),
        policies=PolicySet(d.policy for d in documents if d.policy is not None),
    )


def trace_query(pool: asyncpg.Pool) -> TraceQuery:
    return PostgresEventStore(pool)


def event_writer(pool: asyncpg.Pool) -> EventWriter:
    return PostgresEventStore(pool)


def audit_trail(pool: asyncpg.Pool) -> AuditTrail:
    return AuditTrail(PostgresAuditStore(pool), SystemClock())


def idempotency_store(pool: asyncpg.Pool) -> IdempotencyStore:
    return PostgresIdempotencyStore(pool)


def idempotency_maintenance(
    pool: asyncpg.Pool, settings: IdempotencySettings
) -> IdempotencyMaintenance:
    clock = SystemClock()
    return IdempotencyMaintenance(
        PostgresIdempotencyStore(pool),
        IdempotencyAudit(
            AuditTrail(PostgresAuditStore(pool), clock),
            LogDiagnostics(logging.getLogger("adapter_verify.diagnostics")),
        ),
        LogOwnerAlerts(logging.getLogger("adapter_verify.alerts")),
        clock,
        timing(settings),
    )


# --------------------------------------------------------------------------- golden tasks (§6)


def golden_workspace(settings: GoldenSettings) -> Workspace:
    return load_workspace(settings.root, settings.tasks_dir, settings.definitions_dir)


def golden_layout(settings: GoldenSettings, access: AccessSettings) -> Layout:
    root = settings.root.resolve()

    def rel(path: Path) -> str:
        full = (root / path).resolve()
        return full.relative_to(root).as_posix() if full.is_relative_to(root) else path.as_posix()

    return Layout(
        tasks_dir=rel(settings.tasks_dir),
        definitions_dir=rel(settings.definitions_dir),
        catalog_file=rel(access.catalog_path),
        policies_dir=rel(access.policies_dir),
        quarantine_file=f"{rel(settings.tasks_dir)}/quarantine.yaml",
    )


def golden_digests(workspace: Workspace, policies: PolicySet) -> dict[str, str]:
    definitions = list(workspace.definitions.values())
    agents = workspace.agents
    digests: dict[str, str] = {}
    for task in workspace.tasks:
        policy = policies.get(task.agent)
        digests[task.task_id] = input_digest(
            task,
            definitions,
            workspace.fixture_digests,
            agents.get(task.agent),
            None if policy is None else policy.model_dump(mode="json"),
        )
    return digests


def golden_harness(config: AgentConfig) -> AgentHarness:
    """No production harness exists yet: reference agents arrive with M5b (DESIGN.md D-074)."""
    msg = f"no agent harness of kind {config.harness!r} is available for {config.agent_id}"
    raise HarnessUnavailableError(msg)


def golden_sandboxes(access_settings: AccessSettings, workspace: Workspace) -> "_SandboxFactory":
    return _SandboxFactory(access_settings, workspace)


class _SandboxFactory:
    """A fresh pipeline per run: in-memory stores, sentinel secrets, the real policies."""

    def __init__(self, access_settings: AccessSettings, workspace: Workspace) -> None:
        self._access = access_settings
        self._workspace = workspace

    def __call__(self, task: GoldenTask, run_label: str) -> SandboxSession:
        clock, entropy = SystemClock(), SystemEntropy()
        telemetry, events = RecordingTelemetry(), MemoryEventSink()
        diagnostics, audit_store, alerts = (
            MemoryDiagnostics(),
            MemoryAuditStore(),
            MemoryOwnerAlerts(),
        )
        audit = AuditTrail(audit_store, clock)
        credential = SecretStr(f"golden:{run_label}")
        access = build_access(
            self._access,
            verifier=StaticTokenVerifier({credential.get_secret_value(): task.agent}),
            secrets=SentinelSecretManager(),
            audit=audit,
            telemetry=telemetry,
            events=events,
            diagnostics=diagnostics,
            clock=clock,
        )
        idempotency = build_idempotency(
            IdempotencySettings(),
            store=MemoryIdempotencyStore(),
            payloads=MemoryPayloadStore(),
            audit=audit,
            telemetry=telemetry,
            events=events,
            diagnostics=diagnostics,
            alerts=alerts,
            clock=clock,
            entropy=entropy,
        )
        definitions = self._workspace.definitions
        fixtures = [
            (ref, body)
            for ref in task.fixtures
            if (body := self._workspace.fixture(ref.payload)) is not None
        ]
        backend = FixtureBackend(fixtures, access.credentials)
        stages = stage_order(access, idempotency)
        stages.insert(1, DefinitionInputStage(definitions))  # §1.3: validation after access
        entry = ObservedEntry(
            telemetry=telemetry,
            events=events,
            diagnostics=diagnostics,
            clock=clock,
            entropy=entropy,
            downstream=AuthenticatedPipeline(access.authenticator, stages, backend),
        )
        return GoldenSandbox(
            entry=entry,
            authenticator=access.authenticator,
            credential=credential,
            catalog=load_catalog(self._access.catalog_path),
            definitions=definitions,
            backend=backend,
            sinks=(telemetry, events, diagnostics, audit_store, alerts),
            label=run_label,
        )


def golden_runner(
    settings: GoldenSettings,
    access_settings: AccessSettings,
    workspace: Workspace,
) -> GoldenRunner:
    return GoldenRunner(
        sandboxes=golden_sandboxes(access_settings, workspace),
        harnesses=golden_harness,
        judge=None,  # the LLM judge adapter arrives with M5b
        calibration=load_calibration(settings.root / settings.calibration_dir)
        if (settings.root / settings.calibration_dir).is_dir()
        else [],
        egress=SocketEgressGuard(frozenset(settings.egress_allowed_hosts)),
        clock=SystemClock(),
    )


def git_repo(settings: GoldenSettings) -> GitRepo:
    return GitRepo(settings.root)


def golden_description_diffs(
    repo: GitRepo, base: str, workspace: Workspace, layout: Layout, changed: list[str]
) -> dict[str, str]:
    """tool@version → unified diff of its description since ``base`` (the §6.11 report)."""
    diffs: dict[str, str] = {}
    current: dict[str, ToolDefinition] = workspace.definitions
    for path in changed:
        if not path.startswith(f"{layout.definitions_dir}/") or not path.endswith(".yaml"):
            continue
        tool, _, version = (
            path.removeprefix(f"{layout.definitions_dir}/").removesuffix(".yaml").partition("/")
        )
        new = current.get(f"{tool}@{version}")
        old_text = repo.show(base, path)
        try:
            old = None if old_text is None else parse_yaml_model(ToolDefinition, old_text)
        except ConfigFileError:
            old = None
        before = "" if old is None else old.description
        after = "" if new is None else new.description
        if before != after:
            diffs[f"{tool}@{version}"] = "".join(
                difflib.unified_diff(
                    before.splitlines(keepends=True) or [""],
                    after.splitlines(keepends=True) or [""],
                    fromfile=f"{base}:{path}",
                    tofile=path,
                )
            )
    return diffs
