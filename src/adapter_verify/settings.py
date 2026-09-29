"""Typed configuration, read from the environment and validated at startup (brief §2).

Invalid or missing configuration raises at load time; the process must not start.
"""

from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import (
    BaseSettings,
    DotEnvSettingsSource,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
)


class ObservabilitySettings(BaseSettings):
    """Section 8 settings. Environment prefix ``ADAPTER_OBSERVABILITY_``."""

    model_config = SettingsConfigDict(
        env_prefix="ADAPTER_OBSERVABILITY_", frozen=True, extra="forbid"
    )

    service_name: str = Field(
        default="adapter-verify", min_length=1, description="OTel service.name."
    )
    otlp_endpoint: str | None = Field(
        default=None, description="OTLP/HTTP traces URL. Unset: spans are not exported."
    )
    span_queue_size: Annotated[int, Field(ge=1, le=1_000_000)] = Field(
        default=2048, description="Bounded span export queue; overflow drops spans."
    )
    read_sample_ratio: Annotated[float, Field(ge=0.0, le=1.0)] = Field(
        default=1.0, description="Share of successful read events kept (§8.6). 1.0 keeps all."
    )

    event_buffer_capacity: Annotated[int, Field(ge=1, le=10_000_000)] = Field(
        default=10_000, description="Events buffered before the store; overflow drops reads first."
    )
    event_batch_size: Annotated[int, Field(ge=1, le=100_000)] = Field(
        default=500, description="Events per write to the event store."
    )
    event_flush_interval_s: Annotated[float, Field(gt=0, le=60)] = Field(
        default=0.5, description="Minimum seconds between event-store writes."
    )

    @field_validator("otlp_endpoint")
    @classmethod
    def _http_url(cls, value: str | None) -> str | None:
        if value is not None and not value.startswith(("https://", "http://")):
            msg = "otlp_endpoint must be an http(s) URL"
            raise ValueError(msg)
        return value


class AccessSettings(BaseSettings):
    """Section 9 settings. Environment prefix ``ADAPTER_ACCESS_``."""

    model_config = SettingsConfigDict(env_prefix="ADAPTER_ACCESS_", frozen=True, extra="forbid")

    policies_dir: Path = Field(default=Path("policies"), description="Policy YAML directory.")
    catalog_path: Path = Field(default=Path("catalog/tools.yaml"), description="Tool catalog.")
    credentials_path: Path = Field(
        default=Path("credentials.yaml"), description="Tool to secret-name bindings."
    )
    jwt_issuer: str | None = Field(default=None, description="Expected token issuer (iss).")
    jwt_audience: str | None = Field(default=None, description="Expected token audience (aud).")
    jwks_url: str | None = Field(default=None, description="https URL of the issuer's JWKS.")
    clock_skew_s: Annotated[int, Field(ge=0, le=300)] = Field(
        default=60, description="Allowed clock skew for exp/nbf/iat."
    )
    jwks_ttl_s: Annotated[int, Field(ge=10, le=86_400)] = Field(
        default=300, description="Signing keys older than this are never used."
    )
    secret_cache_ttl_s: Annotated[int, Field(ge=0, le=3_600)] = Field(
        default=60, description="Scoped credential cache lifetime."
    )
    policy_reload_interval_s: Annotated[int, Field(ge=5, le=3_600)] = Field(
        default=30, description="How often policies are re-read; bad sets are rejected."
    )

    @field_validator("jwks_url")
    @classmethod
    def _https(cls, value: str | None) -> str | None:
        if value is not None and not value.startswith("https://"):
            msg = "jwks_url must be https"
            raise ValueError(msg)
        return value


class IdempotencySettings(BaseSettings):
    """Section 7 settings. Environment prefix ``ADAPTER_IDEMPOTENCY_``. Overrides are JSON
    objects keyed by tool name (M3-Q2)."""

    model_config = SettingsConfigDict(
        env_prefix="ADAPTER_IDEMPOTENCY_", frozen=True, extra="forbid"
    )

    lease_s: Annotated[int, Field(ge=1, le=3_600)] = Field(
        default=30, description="Reservation lease. Must exceed the connector timeout."
    )
    retention_h: Annotated[int, Field(ge=1, le=8_760)] = Field(
        default=72, description="Record retention. Never shorter than any agent's retry window."
    )
    lease_overrides_s: dict[str, int] = Field(
        default_factory=dict, description='Per-tool lease, e.g. {"order.cancel": 60}.'
    )
    retention_overrides_h: dict[str, int] = Field(
        default_factory=dict, description="Per-tool retention in hours."
    )


class ContractSettings(BaseSettings):
    """Section 5 settings. Environment prefix ``ADAPTER_CONTRACT_``. Paths are repo-relative."""

    model_config = SettingsConfigDict(env_prefix="ADAPTER_CONTRACT_", frozen=True, extra="forbid")

    root: Path = Field(default=Path(), description="Repository root; sample paths resolve here.")
    sources_dir: Path = Field(default=Path("sources"), description="Upstream source definitions.")
    baselines_dir: Path = Field(default=Path("baselines"), description="Accepted shapes.")
    contracts_dir: Path = Field(default=Path("contracts"), description="Canonical contracts.")
    mappings_dir: Path = Field(default=Path("mappings"), description="Translation mappings.")
    inferred_min_samples: Annotated[int, Field(ge=1)] = Field(
        default=200, description="Samples needed before an observed shape is OBSERVED (M4-Q1)."
    )
    inferred_min_days: Annotated[int, Field(ge=0)] = Field(
        default=7, description="Days the samples must span before a shape is OBSERVED."
    )
    rename_threshold: Annotated[float, Field(gt=0, le=1)] = Field(
        default=0.6, description="Name similarity for FIELD_RENAMED_SUSPECTED."
    )


class _OwnFieldsDotEnv(DotEnvSettingsSource):
    """A shared .env may hold other components' keys; take only this model's own fields."""

    def __call__(self) -> dict[str, Any]:
        fields = self.settings_cls.model_fields
        return {k: v for k, v in super().__call__().items() if k in fields}


_DEFAULT_JUDGE_MODELS: dict[str, str] = {
    "gemini": "gemini-3.8-flash",
    "nvidia": "moonshotai/kimi-k2.6",
}


class GoldenSettings(BaseSettings):
    """Section 6 settings. Environment prefix ``ADAPTER_GOLDEN_``. Paths are repo-relative."""

    # Also read from a git-ignored .env in the working directory (the Gemini key, D-083).
    model_config = SettingsConfigDict(
        env_prefix="ADAPTER_GOLDEN_",
        env_file=".env",
        env_file_encoding="utf-8",
        env_ignore_empty=True,  # a blank key in .env means "not set"
        frozen=True,
        extra="forbid",
    )

    root: Path = Field(default=Path(), description="Repository root.")
    tasks_dir: Path = Field(
        default=Path("golden_tasks"), description="Task files and agent configs."
    )
    definitions_dir: Path = Field(
        default=Path("catalog/definitions"), description="Tool descriptions and inputs."
    )
    calibration_dir: Path = Field(
        default=Path("tests/golden_selftest/calibration"), description="Judge calibration cases."
    )
    results_dir: Path = Field(default=Path("golden-results"), description="Where results go.")
    token_budget: Annotated[int, Field(ge=1)] | None = Field(
        default=None, description="Suite token budget; runs stop when it is spent. Measure first."
    )
    egress_allowed_hosts: tuple[str, ...] = Field(
        default=(), description="Hosts a run may reach, e.g. generativelanguage.googleapis.com."
    )
    gemini_api_key: SecretStr | None = Field(
        default=None, description="Gemini API key for the reference agent and judge. Secret."
    )
    nvidia_api_key: SecretStr | None = Field(
        default=None, description="NVIDIA API catalog key for the agent and judge. Secret."
    )
    nvidia_base_url: str = Field(
        default="https://integrate.api.nvidia.com/v1",
        pattern=r"^https://",
        description="NVIDIA OpenAI-compatible API base URL.",
    )
    judge_provider: Literal["gemini", "nvidia"] = Field(
        default="nvidia", description="Model API of the answer judge (D-095)."
    )
    judge_model: str | None = Field(
        default=None,
        min_length=1,
        description="Pinned judge model. Default per provider: gemini-3.8-flash for Gemini, "
        "moonshotai/kimi-k2.6 for NVIDIA.",
    )

    @property
    def judge_model_id(self) -> str:
        return self.judge_model or _DEFAULT_JUDGE_MODELS[self.judge_provider]

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        del dotenv_settings
        return (init_settings, env_settings, _OwnFieldsDotEnv(settings_cls), file_secret_settings)


class DatabaseSettings(BaseSettings):
    """Postgres connection. Environment prefix ``ADAPTER_DATABASE_``.

    The DSN contains a credential: it is a ``SecretStr`` and must never be printed. Report
    settings errors with ``errors(include_input=False)``.
    """

    model_config = SettingsConfigDict(env_prefix="ADAPTER_DATABASE_", frozen=True, extra="forbid")

    dsn: SecretStr = Field(description="postgresql:// connection string. Required. Secret.")
    pool_min_size: Annotated[int, Field(ge=0, le=100)] = Field(
        default=1, description="Minimum pooled connections."
    )
    pool_max_size: Annotated[int, Field(ge=1, le=500)] = Field(
        default=10, description="Maximum pooled connections."
    )

    @field_validator("dsn")
    @classmethod
    def _postgres_scheme(cls, value: SecretStr) -> SecretStr:
        if not value.get_secret_value().startswith(("postgresql://", "postgres://")):
            msg = "dsn must start with postgresql:// or postgres://"
            raise ValueError(msg)
        return value
