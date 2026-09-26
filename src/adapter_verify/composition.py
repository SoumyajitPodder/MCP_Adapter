"""Composition root: the only module that wires ports to adapters (brief §2).

Stage order is fixed here and will be covered by a test once more stages exist (§3.5).
"""

import logging
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import asyncpg

from adapter_kernel.pipeline import Next, Stage
from adapter_verify.access.adapters.files import (
    load_catalog,
    load_credential_map,
    load_policy_documents,
)
from adapter_verify.access.adapters.jwt import JwksCache, JwtTokenVerifier, https_jwks_fetcher
from adapter_verify.access.domain.lint import PolicyDocument
from adapter_verify.access.domain.policy import PolicySet, ToolCatalog
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
from adapter_verify.common.ports import Clock
from adapter_verify.contract_ci.adapters.files import FileContractRepository
from adapter_verify.contract_ci.domain.extract import EvidenceThreshold
from adapter_verify.contract_ci.service import AccessView, ContractCi
from adapter_verify.observability.adapters.otel import OtelTelemetry, build_tracer_provider
from adapter_verify.observability.adapters.postgres import PostgresAuditStore, PostgresEventStore
from adapter_verify.observability.adapters.stdlog import LogDiagnostics, LogEventSink
from adapter_verify.observability.audit_service import AuditTrail
from adapter_verify.observability.buffered_sink import BufferedEventSink
from adapter_verify.observability.ports import (
    Diagnostics,
    EventSink,
    EventWriter,
    Telemetry,
    TraceQuery,
)
from adapter_verify.observability.service import Downstream, ObservedEntry, SampledEventSink
from adapter_verify.pipeline import AuthenticatedPipeline
from adapter_verify.settings import (
    AccessSettings,
    ContractSettings,
    DatabaseSettings,
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


def stage_order(access: Access) -> list[Stage]:
    """The fixed stage order (§3.5). §4 lifecycle, validation, §7 and §3/§2 stages join here."""
    return [access.stage]


def build_pipeline(access: Access, terminal: Next) -> AuthenticatedPipeline:
    return AuthenticatedPipeline(access.authenticator, stage_order(access), terminal)


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
