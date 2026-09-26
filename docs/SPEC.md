# adapter_verify — Specification (LLD §5–9)

> **Status:** M0, M1 (§8), M2 (§9), M3 (§7), M4 (§5) and M5a (§6, offline core) implemented; M5b (LLM agents and judge) pending. Sections marked *generated* will be produced by `scripts/gen_spec.py` and checked by the `spec-fresh` CI job. Don't edit between the `BEGIN/END GENERATED` markers by hand.
>
> Hand-written sections explain **why**. Generated sections state **what**.

## Contents

1. [Overview](#1-overview)
2. [Shared kernel](#2-shared-kernel)
3. [Error codes](#3-error-codes)
4. [Configuration](#4-configuration)
5. [Contract testing in CI (§5)](#5-contract-testing-in-ci)
6. [Golden-task regression (§6)](#6-golden-task-regression)
7. [Duplicate prevention (§7)](#7-duplicate-prevention)
8. [Correlation-ID logging (§8)](#8-correlation-id-logging)
9. [Baseline access control (§9)](#9-baseline-access-control)
10. [Ports and adapters](#10-ports-and-adapters)
11. [CLI reference](#11-cli-reference)
12. [Database tables](#12-database-tables)

---

## 1. Overview

`adapter_verify` answers one question for every MCP tool call: can we safely and reliably execute and verify it?

- Two components gate deploys: contract CI (§5) and golden tasks (§6).
- Three run on every call: duplicate prevention (§7), correlation-ID logging (§8), and access control (§9).

This document states *what* each one does. `DESIGN.md` records *why*, including rejected options and each decision's approval status.

The process entry point is `ObservedEntry`, which wraps the whole pipeline. It takes a transport-neutral `InboundCall` (tool, arguments, `_meta` strings) and returns a `ToolResult`. Who owns the MCP server that produces `InboundCall`s is still open (DESIGN.md M1-Q1).

## 2. Shared kernel

*Pending Romik and owner agreement (DESIGN.md, M0 §5).* Once frozen, this section holds generated JSON Schemas for `RequestContext`, `CallContext`, `ToolRequest`, `ToolResult`, `ResponseMeta`, `ShapeModel`, and the `Classification`, `Behavior`, `Sensitivity` and `DeliveryStatus` enums.

<!-- BEGIN GENERATED: kernel-models -->
JSON Schemas for every model are in `docs/schemas/`.

### Enums

| Enum | Values | Meaning |
| --- | --- | --- |
| `Classification` | `COMPATIBLE`, `REVIEW_REQUIRED`, `BREAKING`, `UNKNOWN` | How safe a detected change is for existing consumers. |
| `Behavior` | `read_only`, `mutating`, `destructive` | What a tool does to upstream state. Drives idempotency (§7) and scope checks (§9). |
| `Sensitivity` | `public`, `internal`, `pii`, `secret` | Per-field ``x-sensitivity`` annotation used by redaction (§8.4). |
| `DeliveryStatus` | `not_sent`, `sent_no_response`, `acked`, `rejected_no_effect` | What a connector knows about whether a request reached the backend (DESIGN.md R-002). |
| `RetryPolicy` | `never`, `immediate`, `after_delay` | Whether, and when, an agent may retry the same call with the same idempotency key. |
| `FieldType` | `string`, `integer`, `number`, `boolean`, `object`, `array`, `datetime`, `enum`, `null` | Normalized field type across all source kinds. |
| `SourceKind` | `openapi`, `wsdl`, `file`, `queue`, `observed`, `canonical` | Kind of upstream artifact a shape was extracted from. |
| `ProvenanceKind` | `declared`, `observed`, `inferred` | Strength of the evidence behind a shape (brief 5.3). |

### Models

#### `RequestContext`

Everything known about a call before the caller is authenticated.

| Field | Type | Required | Meaning |
| --- | --- | --- | --- |
| `correlation_id` | `str` | yes | Business-level join key (§8). |
| `correlation_id_generated` | `bool` | yes | True when the adapter generated the ID because the caller supplied none. |
| `trace_id` | `str` | yes | Distributed trace ID. |
| `tool` | `str` | yes | Tool name as called, e.g. order.get. |
| `requested_version` | `str \| None` | yes | Version the caller asked for, or None to let the registry resolve it. |
| `idempotency_key` | `str \| None` | yes | Caller-supplied key. Required for state-changing tools (§7). |

#### `CallContext`

Everything known about a call after authentication and version resolution.

| Field | Type | Required | Meaning |
| --- | --- | --- | --- |
| `request` | `RequestContext` | yes | The pre-authentication context. |
| `agent_id` | `str` | yes | Authenticated agent identity (§9). |
| `semantic_version` | `str` | yes | Resolved tool version. |
| `behavior` | `Behavior` | yes | Behavior annotation of the resolved tool. |

#### `ToolRequest`

Validated-shape tool arguments. Carriers such as correlation ID live in the context.

| Field | Type | Required | Meaning |
| --- | --- | --- | --- |
| `arguments` | `JsonObject` | yes | Tool arguments as sent by the agent. |

#### `ToolResult`

Result passed back up the stage chain.

| Field | Type | Required | Meaning |
| --- | --- | --- | --- |
| `outcome` | `ToolSuccess \| ToolFailure` | yes | Success or failure, discriminated by kind. |
| `delivery` | `DeliveryStatus \| None` | no | Connector-reported delivery status. Internal only. |

#### `ToolSuccess`

A successful tool call.

| Field | Type | Required | Meaning |
| --- | --- | --- | --- |
| `kind` | `Literal['success']` | no | Discriminator. |
| `content` | `JsonObject` | yes | Canonical tool output. |
| `meta` | `ResponseMeta` | yes | Adapter metadata. |

#### `ToolFailure`

A failed tool call. The error is always a member of the shared enum.

| Field | Type | Required | Meaning |
| --- | --- | --- | --- |
| `kind` | `Literal['failure']` | no | Discriminator. |
| `error` | `AdapterError` | yes | Agent-facing error. |
| `meta` | `ResponseMeta` | yes | Adapter metadata. |

#### `ResponseMeta`

Adapter metadata attached to every result, success or failure.

| Field | Type | Required | Meaning |
| --- | --- | --- | --- |
| `correlation_id` | `str` | yes | Echoed back (§8). |
| `absorbed` | `tuple[str, ...]` | no | Drift changes absorbed on this call (§3). |
| `warnings` | `tuple[str, ...]` | no | Deprecation or pending-review notices (§3/§4). |
| `replayed` | `bool` | no | True if the result came from the idempotency store (§7). |
| `adapter_version` | `str \| None` | no | Adapter version that served the call (§4). |

#### `AdapterError`

An error as returned to an agent.

| Field | Type | Required | Meaning |
| --- | --- | --- | --- |
| `code` | `ErrorCode` | yes | Member of the shared error enum. |
| `retry_after_ms` | `int \| None` | no | Suggested delay. Set only when the code's retry policy is after_delay. |

#### `ErrorSpec`

Fixed properties of an error code.

| Field | Type | Required | Meaning |
| --- | --- | --- | --- |
| `retry` | `RetryPolicy` | yes | Retry guidance returned to the agent. |
| `owner` | `str` | yes | Brief section that raises this code. |
| `agent_message` | `str` | yes | Fixed agent-facing text. Never interpolated. |

#### `Shape`

Normalized shape of one upstream source. The unit of a baseline.

| Field | Type | Required | Meaning |
| --- | --- | --- | --- |
| `source_id` | `str` | yes | Stable source identifier, e.g. "order-management.rest.get-order". |
| `source_kind` | `SourceKind` | yes | Kind of source the shape was extracted from. |
| `source_version` | `str` | yes | Version of the source artifact. |
| `operations` | `tuple[OperationShape, ...]` | yes | Operations, sorted by name. |
| `provenance` | `Provenance` | yes | Evidence behind the shape. Excluded from the hash. |

#### `OperationShape`

One operation of a source, with fields in canonical order.

| Field | Type | Required | Meaning |
| --- | --- | --- | --- |
| `name` | `str` | yes | Operation name within the source. |
| `inputs` | `tuple[FieldShape, ...]` | yes | Input fields, sorted by path. |
| `outputs` | `tuple[FieldShape, ...]` | yes | Output fields, sorted by path. |
| `errors` | `frozenset[str]` | yes | Declared error identifiers. |

#### `FieldShape`

One field of an operation's input or output.

| Field | Type | Required | Meaning |
| --- | --- | --- | --- |
| `path` | `str` | yes | Field path, e.g. "order.items[].sku". |
| `type` | `FieldType` | yes | Normalized field type. |
| `required` | `bool` | yes | Field must be present. |
| `nullable` | `bool` | yes | Field may be null when present. |
| `enum_values` | `frozenset[str] \| None` | no | Allowed values. Set exactly when type is enum. |
| `format` | `str \| None` | no | date-time, a regex, or a fixed-width layout reference. |

#### `Provenance`

Where a shape came from and how much to trust it.

| Field | Type | Required | Meaning |
| --- | --- | --- | --- |
| `kind` | `ProvenanceKind` | yes | How strong the evidence behind the shape is. |
| `sample_count` | `int \| None` | yes | Samples behind an observed or inferred shape; None when declared. |
| `extractor` | `str` | yes | Extractor that produced the shape. |
| `extractor_version` | `str` | yes | Extractor version. |
| `extracted_at` | `AwareDatetime` | yes | Extraction time, timezone-aware. |
<!-- END GENERATED -->

## 3. Error codes

Every error an agent can receive. The fixed agent-facing message never contains upstream details.

<!-- BEGIN GENERATED: error-codes -->
| Code | Retry | Raised by | Agent-facing message |
| --- | --- | --- | --- |
| `INVALID_INPUT` | `never` | shared | The tool arguments do not match the tool's input schema. |
| `NOT_AUTHORIZED` | `never` | §9 | This agent is not authorized to call this tool. |
| `VERSION_NOT_PERMITTED` | `never` | §9 | This agent is not permitted to call this version of the tool. |
| `SCOPE_VIOLATION` | `never` | §9 | This agent has read-only scope and cannot call a state-changing tool. |
| `IDEMPOTENCY_KEY_REQUIRED` | `never` | §7 | This tool changes state and requires an idempotency key. |
| `IDEMPOTENCY_KEY_CONFLICT` | `never` | §7 | This idempotency key was already used with different arguments. |
| `DUPLICATE_IN_PROGRESS` | `after_delay` | §7 | An identical call is still in progress. Retry after the given delay. |
| `RECONCILIATION_PENDING` | `never` | §7 | The outcome of an earlier identical call is unknown and is being resolved by a person. Do not retry. |
| `DRIFT_BLOCKED` | `never` | §3 | The backend changed in a way the adapter cannot safely absorb. |
| `UPSTREAM_UNAVAILABLE` | `immediate` | §3/§4 | The backend is unavailable. The request was not sent. |
| `CONTRACT_VIOLATION` | `never` | §2 | The backend returned data that does not match the tool's contract. |
| `INTERNAL` | `never` | shared | An internal error occurred. Quote the correlation ID when reporting it. |
<!-- END GENERATED -->

## 4. Configuration

All config comes from `ADAPTER_*` environment variables and is validated at startup. Missing or invalid config stops the process from starting.

<!-- BEGIN GENERATED: config -->
| Environment variable | Type | Default | Required | Meaning |
| --- | --- | --- | --- | --- |
| `ADAPTER_OBSERVABILITY_SERVICE_NAME` | `str` | `'adapter-verify'` | no | OTel service.name. |
| `ADAPTER_OBSERVABILITY_OTLP_ENDPOINT` | `str \| None` | `None` | no | OTLP/HTTP traces URL. Unset: spans are not exported. |
| `ADAPTER_OBSERVABILITY_SPAN_QUEUE_SIZE` | `int` | `2048` | no | Bounded span export queue; overflow drops spans. |
| `ADAPTER_OBSERVABILITY_READ_SAMPLE_RATIO` | `float` | `1.0` | no | Share of successful read events kept (§8.6). 1.0 keeps all. |
| `ADAPTER_OBSERVABILITY_EVENT_BUFFER_CAPACITY` | `int` | `10000` | no | Events buffered before the store; overflow drops reads first. |
| `ADAPTER_OBSERVABILITY_EVENT_BATCH_SIZE` | `int` | `500` | no | Events per write to the event store. |
| `ADAPTER_OBSERVABILITY_EVENT_FLUSH_INTERVAL_S` | `float` | `0.5` | no | Minimum seconds between event-store writes. |
| `ADAPTER_DATABASE_DSN` | `SecretStr` | — | yes | postgresql:// connection string. Required. Secret. |
| `ADAPTER_DATABASE_POOL_MIN_SIZE` | `int` | `1` | no | Minimum pooled connections. |
| `ADAPTER_DATABASE_POOL_MAX_SIZE` | `int` | `10` | no | Maximum pooled connections. |
| `ADAPTER_ACCESS_POLICIES_DIR` | `Path` | `WindowsPath('policies')` | no | Policy YAML directory. |
| `ADAPTER_ACCESS_CATALOG_PATH` | `Path` | `WindowsPath('catalog/tools.yaml')` | no | Tool catalog. |
| `ADAPTER_ACCESS_CREDENTIALS_PATH` | `Path` | `WindowsPath('credentials.yaml')` | no | Tool to secret-name bindings. |
| `ADAPTER_ACCESS_JWT_ISSUER` | `str \| None` | `None` | no | Expected token issuer (iss). |
| `ADAPTER_ACCESS_JWT_AUDIENCE` | `str \| None` | `None` | no | Expected token audience (aud). |
| `ADAPTER_ACCESS_JWKS_URL` | `str \| None` | `None` | no | https URL of the issuer's JWKS. |
| `ADAPTER_ACCESS_CLOCK_SKEW_S` | `int` | `60` | no | Allowed clock skew for exp/nbf/iat. |
| `ADAPTER_ACCESS_JWKS_TTL_S` | `int` | `300` | no | Signing keys older than this are never used. |
| `ADAPTER_ACCESS_SECRET_CACHE_TTL_S` | `int` | `60` | no | Scoped credential cache lifetime. |
| `ADAPTER_ACCESS_POLICY_RELOAD_INTERVAL_S` | `int` | `30` | no | How often policies are re-read; bad sets are rejected. |
| `ADAPTER_IDEMPOTENCY_LEASE_S` | `int` | `30` | no | Reservation lease. Must exceed the connector timeout. |
| `ADAPTER_IDEMPOTENCY_RETENTION_H` | `int` | `72` | no | Record retention. Never shorter than any agent's retry window. |
| `ADAPTER_IDEMPOTENCY_LEASE_OVERRIDES_S` | `dict[str, int]` | `{}` | no | Per-tool lease, e.g. {"order.cancel": 60}. |
| `ADAPTER_IDEMPOTENCY_RETENTION_OVERRIDES_H` | `dict[str, int]` | `{}` | no | Per-tool retention in hours. |
| `ADAPTER_CONTRACT_ROOT` | `Path` | `WindowsPath('.')` | no | Repository root; sample paths resolve here. |
| `ADAPTER_CONTRACT_SOURCES_DIR` | `Path` | `WindowsPath('sources')` | no | Upstream source definitions. |
| `ADAPTER_CONTRACT_BASELINES_DIR` | `Path` | `WindowsPath('baselines')` | no | Accepted shapes. |
| `ADAPTER_CONTRACT_CONTRACTS_DIR` | `Path` | `WindowsPath('contracts')` | no | Canonical contracts. |
| `ADAPTER_CONTRACT_MAPPINGS_DIR` | `Path` | `WindowsPath('mappings')` | no | Translation mappings. |
| `ADAPTER_CONTRACT_INFERRED_MIN_SAMPLES` | `int` | `200` | no | Samples needed before an observed shape is OBSERVED (M4-Q1). |
| `ADAPTER_CONTRACT_INFERRED_MIN_DAYS` | `int` | `7` | no | Days the samples must span before a shape is OBSERVED. |
| `ADAPTER_CONTRACT_RENAME_THRESHOLD` | `float` | `0.6` | no | Name similarity for FIELD_RENAMED_SUSPECTED. |
| `ADAPTER_GOLDEN_ROOT` | `Path` | `WindowsPath('.')` | no | Repository root. |
| `ADAPTER_GOLDEN_TASKS_DIR` | `Path` | `WindowsPath('golden_tasks')` | no | Task files and agent configs. |
| `ADAPTER_GOLDEN_DEFINITIONS_DIR` | `Path` | `WindowsPath('catalog/definitions')` | no | Tool descriptions and inputs. |
| `ADAPTER_GOLDEN_CALIBRATION_DIR` | `Path` | `WindowsPath('tests/golden_selftest/calibration')` | no | Judge calibration cases. |
| `ADAPTER_GOLDEN_RESULTS_DIR` | `Path` | `WindowsPath('golden-results')` | no | Where results go. |
| `ADAPTER_GOLDEN_TOKEN_BUDGET` | `Optional[Annotated[int, FieldInfo(annotation=NoneType, required=True, metadata=[Ge(ge=1)])]]` | `None` | no | Suite token budget; runs stop when it is spent. Measure first. |
| `ADAPTER_GOLDEN_EGRESS_ALLOWED_HOSTS` | `tuple[str, ...]` | `()` | no | Hosts a run may reach (none until an LLM harness exists). |
<!-- END GENERATED -->

## 5. Contract testing in CI

*Milestone M4. Implemented. Acceptance: `tests/unit/contract_ci/test_acceptance.py`.*

- **Sources** (`sources/*.yaml`): `observed` (JSONL samples of a REST backend with no spec) or `file` (declared CSV layout plus CSV samples, read by header name).
  - Samples yield a `Shape`. It is INFERRED below `ADAPTER_CONTRACT_INFERRED_MIN_SAMPLES` samples over `_MIN_DAYS` days, else OBSERVED.
  - Ambiguous samples (conflicting types, mixed date formats, an unreadable header) give `SOURCE_UNPARSEABLE` (UNKNOWN).
- **Differ:** every structural difference maps to exactly one rule below; `UNCLASSIFIED_CHANGE` catches anything else. Tested: an empty diff ⇔ equal content hashes.
  - Suspected renames: a removed and an added field of the same type with path similarity ≥ `ADAPTER_CONTRACT_RENAME_THRESHOLD`.
  - INFERRED shapes turn COMPATIBLE presence findings into REVIEW_REQUIRED.
- **Four checks:**
  - **Upstream:** accepted baseline vs current samples.
  - **Canonical:** `contracts/<tool>/<version>.yaml`. `contracts/released.json` locks released versions (editing or deleting one blocks). A new version is diffed against the previous release: a MAJOR bump passes (the new version starts in draft); a MINOR bump makes `OUTPUT_ENUM_VALUE_ADDED` COMPATIBLE.
  - **Mapping replay:** fails closed (UNKNOWN) while `mappings/` has content and no translation engine exists.
  - **Orphans:** catalog ↔ contracts ↔ credentials ↔ sources.
- **Gate:** the worst status wins. Exit `0` pass, `1` blocked, `2` review required (`0` with `--review-approved`), `3` unknown or tool error.
- **Report:** Markdown written to stdout, `--report`, and `$GITHUB_STEP_SUMMARY`. It lists affected tools and the agents granted them.
- **Baselines:** `baselines/<source_id>/<version>.shape.json` carry their content hash; a hand-edited baseline fails. They change only via `baseline accept` in a reviewed PR.

### 5.1 Diff rules
<!-- BEGIN GENERATED: rules -->
| Rule | Direction | Upstream | Canonical | Detects |
| --- | --- | --- | --- | --- |
| `OPERATION_REMOVED` | none | BREAKING | BREAKING | operation present in base, absent in revision |
| `OPERATION_ADDED` | none | COMPATIBLE | COMPATIBLE | operation absent in base, present in revision |
| `OUTPUT_FIELD_REMOVED` | output | BREAKING | BREAKING | output field present in base, absent in revision |
| `OUTPUT_FIELD_ADDED` | output | COMPATIBLE | COMPATIBLE | output field absent in base, present in revision (presence) |
| `INPUT_REQUIRED_FIELD_ADDED` | input | BREAKING | BREAKING | new required input field |
| `INPUT_OPTIONAL_FIELD_ADDED` | input | COMPATIBLE | COMPATIBLE | new optional input field (presence) |
| `INPUT_FIELD_REMOVED` | input | BREAKING | BREAKING | input field present in base, absent in revision |
| `TYPE_CHANGED` | both | BREAKING | BREAKING | field type differs |
| `OUTPUT_BECAME_OPTIONAL` | output | BREAKING | BREAKING | required output field became optional |
| `OUTPUT_BECAME_REQUIRED` | output | COMPATIBLE | COMPATIBLE | optional output field became required (presence) |
| `INPUT_BECAME_REQUIRED` | input | BREAKING | BREAKING | optional input field became required |
| `INPUT_BECAME_OPTIONAL` | input | COMPATIBLE | COMPATIBLE | required input field became optional (presence) |
| `OUTPUT_BECAME_NULLABLE` | output | REVIEW_REQUIRED | REVIEW_REQUIRED | output field may now be null |
| `OUTPUT_BECAME_NON_NULLABLE` | output | COMPATIBLE | COMPATIBLE | output field may no longer be null |
| `INPUT_BECAME_NULLABLE` | input | COMPATIBLE | COMPATIBLE | input field now accepts null |
| `INPUT_BECAME_NON_NULLABLE` | input | BREAKING | BREAKING | input field no longer accepts null |
| `OUTPUT_ENUM_VALUE_ADDED` | output | REVIEW_REQUIRED | REVIEW_REQUIRED | new value in an output enum (MINOR-bump COMPATIBLE) |
| `OUTPUT_ENUM_VALUE_REMOVED` | output | COMPATIBLE | COMPATIBLE | output enum lost a value |
| `INPUT_ENUM_VALUE_ADDED` | input | COMPATIBLE | COMPATIBLE | input enum gained a value |
| `INPUT_ENUM_VALUE_REMOVED` | input | BREAKING | BREAKING | input enum lost a value |
| `FORMAT_CHANGED` | both | REVIEW_REQUIRED | BREAKING | format of a field changed (e.g. date layout) |
| `ERROR_ADDED` | none | REVIEW_REQUIRED | REVIEW_REQUIRED | new declared error |
| `ERROR_REMOVED` | none | COMPATIBLE | COMPATIBLE | declared error no longer returned |
| `FIELD_RENAMED_KNOWN` | both | COMPATIBLE | BREAKING | removed plus added field linked by a registered alias |
| `FIELD_RENAMED_SUSPECTED` | both | REVIEW_REQUIRED | BREAKING | removed plus added field of the same type with similar names and no alias |
| `SOURCE_UNPARSEABLE` | none | UNKNOWN | UNKNOWN | the source could not be extracted into a shape |
| `UNCLASSIFIED_CHANGE` | none | UNKNOWN | UNKNOWN | shapes differ but no rule matched |

**Check-level rule IDs:** `BASELINE_MISSING`, `CONTRACT_RELEASED_VERSION_DELETED`, `CONTRACT_RELEASED_VERSION_EDITED`, `MAPPING_REPLAY_UNAVAILABLE`, `ORPHAN_CONTRACT_WITHOUT_TOOL`, `ORPHAN_CREDENTIAL_WITHOUT_TOOL`, `ORPHAN_SOURCE_WITHOUT_TOOL`, `ORPHAN_TOOL_WITHOUT_CONTRACT`, `ORPHAN_TOOL_WITHOUT_CREDENTIAL`, `SOURCE_UNPARSEABLE`.
<!-- END GENERATED -->

### 5.2 Merge gate and exit codes

See the gate bullet above and §11 (`contract check`).

## 6. Golden-task regression

*Milestone M5a (offline core) implemented. M5b (a Gemini reference agent and judge, D-083) is planned in DESIGN.md session 17, pending the SDK dependency and model pins. Tests: `tests/unit/golden/`, `tests/unit/cli/test_golden_cli.py`.*

- **Task files:** `golden_tasks/<agent>/<task_id>.yaml` (schema below), one `agent.yaml` per agent, and `golden_tasks/quarantine.yaml`.
  - Fixtures are canonical tool output in `fixtures/golden/<name>.synthetic.json`, checked against the tool's contract.
  - `golden lint` runs in CI.
- **Tool definitions:** `catalog/definitions/<tool>/<version>.yaml` holds the description and inputs an agent sees in `tools/list`. This is a synthetic stand-in for the registry.
  - Definitions are outside the released-contract lock: a description edit needs no version bump (§6.9), but it re-runs the tasks that touch the tool.
- **Sandbox:** every run gets a fresh copy of the real pipeline: authentication → `access.check` → `input.validate` (the definition) → `idempotency.reserve` → stub backend.
  - Stores are in memory and the secret manager returns sentinels. The stub backend serves fixtures and counts calls and acknowledged writes.
  - Outbound connections are blocked by an in-process tripwire, not isolation: native code and child processes aren't covered.
- **Agent:** runs behind `AgentHarness`. It sees only the tools `tools/list` allows. Calls are recorded by the sandbox, not taken from the agent.
  - No production harness exists yet, so `golden run` exits 3 naming the agent. `ScriptedAgent` drives the tests.
- **Layers, in order, first failure wins:**
  1. **calls:** tool, version, arguments (JSON equality);
  2. **sequence:** forbidden tools (even denied attempts), order, `exact_calls`;
  3. **state:** writes and per-tool calls at the stub backend;
  4. **answer:** the judge. Without one, runs are `not_judged` and the task is `incomplete`, which never passes a gate.
- **Run errors:** `budget_exceeded` (calls, time, tokens, suite budget), `egress_blocked`, `sentinel_leak`, `harness_error`, `judge_error`. An error is a failed run, never retried.
- **Verdict:** `pass` at `threshold` passing runs. Tasks that write must pass every run (lint). A task that fails twice in a row on unchanged inputs is reported as *quarantine recommended*; quarantine itself is a reviewed PR.
- **Selection:** `--changed-since <ref>` re-runs the tasks whose definition, fixture, task file, agent config or policy changed. Adapter code, catalog or contract changes re-run everything.
- **Results:** a JSON file per suite run (`SuiteResults`, schema version 1), plus a Markdown report showing expected vs actual per failure and a diff of every changed tool description.
- **Gate:** `golden gate --tool T --version V --results F` passes only on current (same input digest), passing evidence for every task touching `T@V`. Missing, stale, incomplete or zero coverage fails. A quarantined task blocks unless a waiver names the tool.

### 6.1 Task file schema
<!-- BEGIN GENERATED: golden-task-schema -->
JSON Schemas: `docs/schemas/GoldenTask.json`, `AgentConfig.json`, `QuarantineList.json`, `ToolDefinition.json`, `CalibrationCase.json`, `SuiteResults.json` (results schema version 1).

#### `GoldenTask`

One golden task (§6.1). Unknown keys are rejected.

| Field | Type | Required | Meaning |
| --- | --- | --- | --- |
| `task_id` | `str` | yes | Unique; equals the file name. |
| `agent` | `str` | yes | Agent under test; the directory. |
| `description` | `str` | yes | What the task proves. |
| `prompt` | `str` | yes | What the agent is asked. |
| `fixtures` | `tuple[FixtureRef, ...]` | no | Stub backend answers. |
| `expect_calls` | `tuple[ExpectedCall, ...]` | no | Required calls. |
| `ordered` | `bool` | no | expect_calls must occur in this order (other calls may interleave). |
| `exact_calls` | `bool` | no | No calls beyond expect_calls. |
| `expect_no_calls` | `tuple[str, ...]` | no | Tools the agent must not even attempt. |
| `expect_state` | `ExpectedState \| None` | no | Stub backend end state. |
| `expect_answer` | `ExpectedAnswer \| None` | no | Judged final answer. |
| `runs` | `int` | yes | Repeats per suite run. |
| `threshold` | `int` | yes | Passing runs needed. |
| `tags` | `tuple[str, ...]` | no | Free-form labels. |

#### `FixtureRef`

What the stub backend returns for calls to one tool (canonical output, synthetic).

| Field | Type | Required | Meaning |
| --- | --- | --- | --- |
| `tool` | `str` | yes | Tool the fixture answers for. |
| `source_id` | `str \| None` | no | Upstream source it stands for (§5); used once connectors exist. |
| `payload` | `str` | yes | Repo-relative path: fixtures/golden/<name>.synthetic.json. |
| `when` | `JsonObject \| None` | no | Serve only for calls whose arguments contain these values; None: any call. |

#### `ExpectedCall`



| Field | Type | Required | Meaning |
| --- | --- | --- | --- |
| `tool` | `str` | yes | Tool the agent must call. |
| `semantic_version` | `str \| None` | no | Required resolved version; None accepts any. |
| `args` | `JsonObject` | no | Expected arguments. |
| `args_match` | `ArgsMatch` | no | exact: arguments equal; subset: these keys with these values. JSON equality. |

#### `ExpectedState`



| Field | Type | Required | Meaning |
| --- | --- | --- | --- |
| `writes_performed` | `int \| None` | no | State-changing calls the stub backend acknowledged. |
| `calls_by_tool` | `dict[str, int]` | no | Calls that reached the stub backend, per tool. |

#### `ExpectedAnswer`



| Field | Type | Required | Meaning |
| --- | --- | --- | --- |
| `rubric` | `str` | yes | What a correct final answer says (§6.5). |

#### `AgentConfig`

``golden_tasks/<agent_id>/agent.yaml``: how to run one agent under test.

| Field | Type | Required | Meaning |
| --- | --- | --- | --- |
| `agent_id` | `str` | yes | Agent; equals the directory. |
| `harness` | `str` | yes | Harness kind that runs the agent. |
| `model` | `str \| None` | no | Pinned model ID, recorded with results. |
| `limits` | `RunLimits` | no | Per-run limits. |

#### `RunLimits`

Per-run limits (§6.3). Crossing any is BUDGET_EXCEEDED, never retried.

| Field | Type | Required | Meaning |
| --- | --- | --- | --- |
| `max_tool_calls` | `int` | no | Calls per run. |
| `timeout_s` | `float` | no | Wall-clock per run. |
| `max_tokens` | `int \| None` | no | Tokens per run, as reported by the harness. |

#### `QuarantineEntry`



| Field | Type | Required | Meaning |
| --- | --- | --- | --- |
| `task_id` | `str` | yes | Quarantined task. |
| `owner` | `str` | yes | Who fixes it. |
| `reason` | `str` | yes | Why it is quarantined. |
| `since` | `str` | yes | YYYY-MM-DD, quoted. |
| `waiver` | `Waiver \| None` | no | Promotion waiver, if any. |

#### `Waiver`

Lets named tools be promoted while a task that touches them is quarantined (R-013).

| Field | Type | Required | Meaning |
| --- | --- | --- | --- |
| `tools` | `tuple[str, ...]` | yes | Tools released from the block. |
| `approved_by` | `str` | yes | Who accepted the risk. |
| `reason` | `str` | yes | Why. |

#### `ToolDefinition`

What ``tools/list`` shows an agent for one tool version.

| Field | Type | Required | Meaning |
| --- | --- | --- | --- |
| `tool` | `str` | yes | Tool name. |
| `version` | `str` | yes | Tool version. |
| `description` | `str` | yes | Agent-facing description. |
| `inputs` | `tuple[InputField, ...]` | no | Arguments. |

#### `InputField`

One tool argument.

| Field | Type | Required | Meaning |
| --- | --- | --- | --- |
| `name` | `str` | yes | Argument name. |
| `type` | `ContractType` | yes | Canonical type. |
| `values` | `tuple[str, ...] \| None` | no | Allowed values for enums. |
| `required` | `bool` | no | Whether the argument must be present. |
| `description` | `str` | no | What the argument means, for the agent. |

| Lint rule | Fails when |
| --- | --- |
| `GOLDEN_SCHEMA` | a task, agent config, definition or quarantine file does not validate |
| `GOLDEN_FILE_NAME_MISMATCH` | files must be <agent>/<task_id>.yaml and <agent>/agent.yaml |
| `GOLDEN_DUPLICATE_ID` | two tasks share a task_id |
| `GOLDEN_UNKNOWN_TOOL` | a fixture or expected call names a tool that is not in the catalog |
| `GOLDEN_UNKNOWN_VERSION` | an expected call pins a version that is not in the catalog |
| `GOLDEN_UNKNOWN_AGENT` | the task's agent has no access policy |
| `GOLDEN_AGENT_CONFIG_MISSING` | the task's agent has no agent.yaml |
| `GOLDEN_TOOL_NOT_GRANTED` | an expected call is not granted to the agent (can never pass) |
| `GOLDEN_EXPECT_CONFLICT` | a tool is both expected and forbidden |
| `GOLDEN_ARGS_INVALID` | expected arguments violate the tool's input definition |
| `GOLDEN_FIXTURE_MISSING` | a fixture file is absent or not a JSON object |
| `GOLDEN_FIXTURE_NOT_SYNTHETIC` | fixtures must be fixtures/golden/<name>.synthetic.json |
| `GOLDEN_FIXTURE_CONTRACT` | a fixture does not match the tool's canonical contract |
| `GOLDEN_THRESHOLD` | threshold exceeds runs, or a writing task doesn't require every run |
| `GOLDEN_DEFINITION_MISSING` | a catalog tool version has no definition |
| `GOLDEN_DEFINITION_ORPHAN` | a definition has no catalog entry, or its file name is wrong |
| `GOLDEN_QUARANTINE_UNKNOWN_TASK` | quarantine.yaml names a task that doesn't exist |
<!-- END GENERATED -->

## 7. Duplicate prevention

*Milestone M3. Implemented. Acceptance: `tests/integration/test_acceptance_idempotency.py` (50 concurrent calls, one upstream call; key conflict) and `tests/integration/test_idempotency_crash.py` (process killed mid-call). State machine: `tests/property/test_idempotency_state_machine.py`.*

- **Which calls:** `IdempotencyStage` runs after access control (lifecycle and input validation will sit between them). Read-only tools pass through.
- **Key:** state-changing tools need `_meta["adapter/idempotency-key"]` matching `[A-Za-z0-9._:-]{1,128}`.
  - A missing key gets `IDEMPOTENCY_KEY_REQUIRED`; an invalid one gets `INVALID_INPUT`. Keys are never derived or truncated.
  - Keys are scoped by `(agent_id, tool, key)`.
- **Fingerprint:** SHA-256 of the arguments in RFC 8785 (JCS) form, so key order and whitespace don't matter.
  - Arguments with no JCS form (non-finite floats, integers beyond ±2^53) get `INVALID_INPUT`.
  - The same key with other arguments, or with another tool version, gets `IDEMPOTENCY_KEY_CONFLICT`.
- **Reserve:** one `INSERT … ON CONFLICT DO NOTHING`. Re-reserving a `FAILED_RETRYABLE` row is a conditional `UPDATE` that rotates `attempt_id`.
  - The reservation is audited before anything is sent. If that write fails, the key is released and the agent gets `INTERNAL`.
- **Existing key** (§7.5):

| Stored | Agent gets | Upstream call |
| --- | --- | --- |
| `COMPLETED` | the stored result or error, `_meta.replayed = true` | no |
| `RESERVED`, lease running | `DUPLICATE_IN_PROGRESS`, `retry_after_ms` = remaining lease (at least 100) | no |
| `RESERVED`, lease expired | `RECONCILIATION_PENDING` | no |
| `UNKNOWN` | `RECONCILIATION_PENDING` | no |
| `FAILED_RETRYABLE` | a new attempt | yes |

- **Settle** (from the connector's `DeliveryStatus`):

| Outcome | New state | Agent gets | Alert |
| --- | --- | --- | --- |
| success | `COMPLETED`, result in the payload store | the result | `success_without_ack` if not `ACKED` |
| failure, delivery `None`, `NOT_SENT` or `REJECTED_NO_EFFECT` | `FAILED_RETRYABLE` | the failure | — |
| failure, `SENT_NO_RESPONSE`; or an exception | `UNKNOWN` | `RECONCILIATION_PENDING` (an exception is re-raised: `INTERNAL`) | `outcome_unknown` |
| failure, `ACKED` | `COMPLETED` with `error_code` | the error; `INTERNAL` if the error would invite a retry | `effect_with_error` |

- **Fencing:** every settlement applies only while the row still holds this attempt's `attempt_id`.
  - If the sweeper moved the row to `UNKNOWN` first, the agent gets `RECONCILIATION_PENDING`.
  - Exception: an `ACKED` completion by the same attempt moves `UNKNOWN → COMPLETED`, and the move is audited.
- **Failure handling:**
  - A result that can't be stored becomes `UNKNOWN`.
  - A settlement that can't be written leaves the row to expire into `UNKNOWN` (`settlement_failed`).
  - A stored result that can't be read back gives `INTERNAL` (`replay_unavailable`).
  - None of these ever re-executes.
- **Timing:** `ADAPTER_IDEMPOTENCY_LEASE_S` (default 30) and `_RETENTION_H` (default 72), with per-tool overrides. Retention must exceed every lease.
  - Timestamps come from the adapter's clock, so instances need synchronized clocks.
  - TODO(owner): check at startup that each lease exceeds its connector timeout, once connector configuration exists.
- **Operations:**
  - `idem sweep`: expired `RESERVED` → `UNKNOWN`, audited and alerted. Run every 15 s or so.
  - `idem purge`: deletes expired `COMPLETED`/`FAILED_RETRYABLE` rows, never `UNKNOWN`. Run daily.
  - `idem unknown`: lists rows awaiting a person.
  - `idem resolve --as completed|failed --reason … --operator …`: records the reason and the OS user on the operator's audit chain, and restarts retention.
  - A row resolved as `completed` replays an empty result with warning `idempotency:resolved-manually`.
- **Audit:** every transition is recorded as `idempotency_transition`, on the agent's chain or on `system:idempotency-sweeper`. Manual resolutions are `manual_reconciliation`.
- **Spans:** `idempotency.reserve` (admission) and `idempotency.settle` (outcome), each with an event carrying `idempotency_state` or `replayed`.
- **Upstream passthrough:** connectors read the key from `CallContext`; forwarding it as `Idempotency-Key` is a connector concern (§7.9).

### 7.1 State machine

```
             ┌────────────── lease expires (idem sweep) ─────────┐
             │                                                   ▼
[RESERVED] ──┼── success, or ACKED failure ──► [COMPLETED]     [UNKNOWN] ── idem resolve ──► COMPLETED | FAILED_RETRYABLE
             ├── provably not sent ─────────► [FAILED_RETRYABLE] ── retry re-reserves ──► RESERVED
             └── sent, no answer; exception ─► [UNKNOWN]      (UNKNOWN ── late ACKED, same attempt ──► COMPLETED)
```

<!-- BEGIN GENERATED: idempotency-record -->
JSON Schema: `docs/schemas/IdempotencyRecord.json`.

#### `IdempotencyRecord`

One idempotency record: brief §7.7 plus the fencing token and the tool version.

| Field | Type | Required | Meaning |
| --- | --- | --- | --- |
| `key` | `RecordKey` | yes | Agent, tool and caller key. |
| `fingerprint` | `bytes` | yes | SHA-256 of the RFC 8785 arguments. |
| `state` | `IdemState` | yes | RESERVED, COMPLETED, FAILED_RETRYABLE or UNKNOWN. |
| `semantic_version` | `str` | yes | Tool version of the attempt. |
| `attempt_id` | `UUID` | yes | Fencing token of the attempt that holds the row. |
| `correlation_id` | `str` | yes | Correlation ID of that attempt. |
| `result_ref` | `str \| None` | yes | Payload-store reference; bodies never live here. |
| `error_code` | `ErrorCode \| None` | yes | Post-acknowledgement failure (R-004). |
| `lease_expires_at` | `AwareDatetime \| None` | yes | Set while RESERVED. |
| `expires_at` | `AwareDatetime` | yes | End of retention; purge never deletes UNKNOWN. |
| `created_at` | `AwareDatetime` | yes | First reservation. |
| `updated_at` | `AwareDatetime` | yes | Last state change. |

Owner alert kinds: `outcome_unknown`, `lease_expired`, `effect_with_error`, `success_without_ack`, `settlement_failed`, `replay_unavailable`.
<!-- END GENERATED -->

## 8. Correlation-ID logging

*Milestone M1. Implemented. Acceptance tests: `tests/integration/test_acceptance_observability.py`, `tests/integration/test_cli_postgres.py`.*

### Propagation

- **Correlation ID:** `ObservedEntry` reads it from `_meta["adapter/correlation-id"]`.
  - If present, it must match `[A-Za-z0-9._:-]{1,128}`. A non-matching ID is **rejected** with `INVALID_INPUT` and never replaced.
  - If absent, the adapter generates a UUIDv7 (RFC 9562), sets `correlation_id_generated`, and echoes the ID in `_meta.correlation_id`.
- **Idempotency key and requested version:** read from `_meta["adapter/idempotency-key"]` and `_meta["adapter/tool-version"]`. The `adapter/` prefix is a placeholder pending the organization's namespace.
- **Request context:** the `RequestContext` is bound to a `ContextVar` for the duration of the call.
- **Unexpected exceptions:** any exception from downstream becomes `INTERNAL`. `Diagnostics` receives only a log-safe summary: exception type and code locations, never the message.

### Redaction (fail closed)

- Bodies never appear in spans, events or logs. They go to the `PayloadStore`, and events carry only `payload_ref`.
- If a body must be shown (debugging, previews), `RedactionPolicy.from_schema(schema)` applies these rules:
  - A leaf is shown only if its own path is annotated `x-sensitivity: public` or `internal`. Container annotations do not propagate to children.
  - `pii` and `secret` leaves become `<redacted:pii>` / `<redacted:secret>`. Declared but unannotated leaves become `<redacted:unannotated>`.
  - Object keys the schema does not declare are dropped and counted in `<redacted:unknown-keys>`.
  - Only `properties` and `items` are followed. Anything under `$ref`/`oneOf`/`allOf` stays masked.
- Span attributes are a closed, typed set (`SpanAttributes`). There is no free-form attribute API.
- OTel exception recording is disabled.

### Sampling and export

- **Never sampled:** events with a non-ok outcome, a state-changing `behavior`, a `drift_classification`, or an `idempotency_state`. Successful reads are kept with probability `ADAPTER_OBSERVABILITY_READ_SAMPLE_RATIO` (default 1.0).
- **Event buffer:** events go through a bounded buffer (`BufferedEventSink`) to the `call_events` table. `emit` never blocks.
  - On overflow, sampled events are evicted first.
  - Must-keep events are dropped only when nothing else can be evicted. Each such episode raises one diagnostic.
- **Spans:** exported by the OTel batch processor over OTLP/HTTP, when an endpoint is configured.

### Audit trail

- **Chains:** security events are appended to hash chains in `audit_log`, one chain per principal (`agent:<id>`, `operator:<id>`, `system:<id>`). The granularity is pending DESIGN.md M1-Q6 and is set in one function, `chain_id_for`.
- **Record hash:** `SHA-256(canonical_json({chain_id, seq, prev_hash, event}))`.
- **Append:** takes a per-chain advisory lock, so there is no global lock.
- **Append-only:** a trigger rejects `UPDATE`/`DELETE`/`TRUNCATE`.
- **Anchoring:** `adapter-verify audit anchor` records every moved chain head in `anchor:global`.
- **Verification:** `adapter-verify audit verify` detects:
  - edited records;
  - deleted or reordered records;
  - records moved between chains;
  - truncation or deletion behind an anchor;
  - malformed anchors.
- **Known limit:** truncating a chain's tail after its latest anchor is not detectable.
- **Failure handling:** `AuditUnavailableError` carries no detail. Callers performing mutating calls must fail closed (M1-Q2).

### 8.1 Event schema
<!-- BEGIN GENERATED: event-schema -->
Current `schema_version`: **1**. JSON Schema: `docs/schemas/CallEvent.json`.

#### `CallEvent`

One observable step of one tool call.

| Field | Type | Required | Meaning |
| --- | --- | --- | --- |
| `schema_version` | `Literal[1]` | no | Bumped on any change. |
| `timestamp` | `AwareDatetime` | yes | When the step finished. |
| `trace_id` | `str` | yes | Distributed trace ID. |
| `span_id` | `str` | yes | Span that emitted the event. |
| `correlation_id` | `str` | yes | Business-level join key. |
| `agent_id` | `str \| None` | yes | Authenticated agent; None before §9 runs. |
| `tool` | `str` | yes | Tool name. |
| `semantic_version` | `str \| None` | yes | Resolved version; None before resolution. |
| `stage` | `SpanName` | yes | Stage that emitted the event. |
| `behavior` | `Behavior \| None` | no | Tool behavior, once known. |
| `backend` | `str \| None` | no | Upstream backend id. |
| `interface` | `str \| None` | no | rest, soap, file or queue. |
| `adapter_version` | `str \| None` | no | Serving adapter version. |
| `latency_ms` | `float \| None` | no | Step latency. |
| `outcome` | `Outcome` | yes | Step outcome. |
| `error_code` | `ErrorCode \| None` | no | Set exactly when the outcome is not ok. |
| `idempotency_key` | `str \| None` | no | Links to §7. |
| `idempotency_state` | `str \| None` | no | §7 state change, if any. |
| `replayed` | `bool \| None` | no | Served from the §7 store. |
| `drift_classification` | `Classification \| None` | no | Drift classification (§3), if drift was handled. |
| `mapping_id` | `str \| None` | no | Mapping used (§3). |
| `payload_ref` | `str \| None` | no | Pointer into the payload store. Never the body. |

Span names: `tool.call`, `access.authenticate`, `access.check`, `lifecycle.gate`, `input.validate`, `idempotency.reserve`, `idempotency.settle`, `backend.call`, `drift.absorb`, `output.validate`.

#### `SpanAttributes`

Everything a span may carry. Identifiers and metadata only, never bodies.

| Field | Type | Required | Meaning |
| --- | --- | --- | --- |
| `correlation_id` | `str \| None` | no | Business join key. |
| `agent_id` | `str \| None` | no | Authenticated agent. |
| `tool` | `str \| None` | no | Tool name. |
| `semantic_version` | `str \| None` | no | Resolved tool version. |
| `outcome` | `Outcome \| None` | no | Stage outcome. |
| `error_code` | `ErrorCode \| None` | no | Shared error code. |
| `rule_id` | `str \| None` | no | Access rule that decided (§9). |
| `adapter_version` | `str \| None` | no | Serving adapter version. |
| `adapter_state` | `str \| None` | no | Lifecycle state (§4). |
| `failing_path` | `str \| None` | no | Schema path that failed validation. A path, never a value. |
| `idempotency_state` | `str \| None` | no | §7 record state. |
| `replayed` | `bool \| None` | no | Result served from §7 store. |
| `backend` | `str \| None` | no | Upstream backend id. |
| `interface` | `str \| None` | no | rest, soap, file or queue. |
| `latency_ms` | `float \| None` | no | Stage latency. |
| `drift_classification` | `Classification \| None` | no | Drift classification (§3). |
| `mapping_id` | `str \| None` | no | Mapping used (§3). |
| `absorbed_fields` | `tuple[str, ...] \| None` | no | Field paths absorbed by drift handling. |
| `quarantine_id` | `str \| None` | no | Output quarantine ref (§2). |

Mirrored semconv names are pinned to `semantic-conventions-genai@8ffdf568e1b4`.
<!-- END GENERATED -->

### 8.2 Span attributes

Every `SpanAttributes` field is emitted as `adapter.<field>`, the source of truth for dashboards. `tool.call` spans also carry:

- `gen_ai.operation.name = execute_tool`
- `gen_ai.tool.name`
- `mcp.method.name = tools/call`

These names were verified against the pinned `semantic-conventions-genai` commit. They are all Development stability. `gen_ai.tool.call.arguments` / `.result` are never emitted: they would carry bodies.

## 9. Baseline access control

*Milestone M2. Implemented. Acceptance: `tests/integration/test_acceptance_access.py`.*

- **Authentication** (`Authenticator`, span `access.authenticate`):
  - The bearer token arrives as `InboundCall.credential`. JWTs are verified against JWKS: `RS256`/`ES256`/`EdDSA` only; `iss`, `aud`, `exp`, `iat`, `sub` required; skew up to `ADAPTER_ACCESS_CLOCK_SKEW_S`.
  - A key set older than `ADAPTER_ACCESS_JWKS_TTL_S` is never used.
  - `sub` is the agent ID.
  - The tool version is resolved from the catalog (default when none is requested).
  - Unknown tools get `NOT_AUTHORIZED`, the same as ungranted ones.
- **Authorization** (`AccessStage`, the first stage; span `access.check`): the pure `evaluate()` over the §9.6 table, returning a decision with a rule ID and `policy_digest`. `tools/list` uses `visible_tools()`, which by construction equals what `tools/call` allows.
- **Deny by default:** no wildcards, exact tool names. Grants never match pre-release versions, and every range must stay inside one MAJOR.
- **Audit:** every denial, and every allow of a state-changing tool. If the audit write fails, a denial still denies and an allow becomes `INTERNAL`.
- **Policies:** loading lints the whole set. Findings refuse startup; on reload the last good set is kept, with one diagnostic.
- **Credentials:** `CredentialScoper` maps tool + version → secret name (`credentials.yaml`) → `SecretManager`, cached for `ADAPTER_ACCESS_SECRET_CACHE_TTL_S`. A tool with no binding gets no credential.

### 9.1 Policy file schema
<!-- BEGIN GENERATED: policy-schema -->
JSON Schemas: `docs/schemas/Policy.json`, `ToolCatalog.json`, `CredentialMap.json`.

#### `Policy`

Everything one agent may do. Deny by default: anything not granted is refused.

| Field | Type | Required | Meaning |
| --- | --- | --- | --- |
| `agent_id` | `str` | yes | Authenticated agent identity. |
| `owner` | `str` | yes | Team accountable for this agent. Required for write scope. |
| `scope` | `Scope` | yes | read agents may never call state-changing tools. |
| `grants` | `tuple[Grant, ...]` | yes | Tool and version grants. |

#### `Grant`

Permission to call one tool within one version range. Tool names match exactly.

| Field | Type | Required | Meaning |
| --- | --- | --- | --- |
| `tool` | `str` | yes | Exact tool name; wildcards are a lint error. |
| `versions` | `str` | yes | SemVer range, e.g. ">=1.0.0,<2.0.0". |

#### `CatalogEntry`

One released tool version. Stand-in for the tool registry (brief §1) until it exists.

| Field | Type | Required | Meaning |
| --- | --- | --- | --- |
| `tool` | `str` | yes |  |
| `version` | `str` | yes | SemVer version. |
| `behavior` | `Behavior` | yes |  |
| `default` | `bool` | no | Served when the caller names no version. |

#### `CredentialBinding`



| Field | Type | Required | Meaning |
| --- | --- | --- | --- |
| `tool` | `str` | yes |  |
| `versions` | `str` | yes | SemVer range this credential serves. |
| `secret_name` | `str` | yes | Name, never value. |

**Decision rule IDs:** `ACCESS_GRANTED`, `ACCESS_NO_POLICY`, `ACCESS_SCOPE_READ_ONLY`, `ACCESS_TOKEN_INVALID`, `ACCESS_TOKEN_MISSING`, `ACCESS_TOOL_NOT_GRANTED`, `ACCESS_UNKNOWN_TOOL`, `ACCESS_VERSION_OUT_OF_RANGE` (`ACCESS_GRANTED:<agent>/<tool>/<grant-index>` when allowed).

| Lint rule | Fails when |
| --- | --- |
| `POLICY_INVALID_FILE` | file is not a valid policy |
| `POLICY_FILE_NAME_MISMATCH` | file name must equal <agent_id>.yaml |
| `POLICY_DUPLICATE_AGENT` | two files declare the same agent_id |
| `POLICY_WILDCARD` | tool names must be exact; wildcards are forbidden |
| `POLICY_UNKNOWN_TOOL` | grant names a tool that is not in the catalog |
| `POLICY_UNKNOWN_VERSION` | grant range matches no released version |
| `POLICY_CROSS_MAJOR` | range must be bounded inside one MAJOR (>=N.x.y,<N+1.0.0) |
| `POLICY_WRITE_WITHOUT_OWNER` | write scope requires an owner |
| `POLICY_READ_SCOPE_MUTATING_GRANT` | read scope granted a state-changing tool |
<!-- END GENERATED -->

## 10. Ports and adapters

| Port | Module | Adapters | Fake (shipped in package) | Added |
| --- | --- | --- | --- | --- |
| `Clock`, `Entropy` | `common.ports` | `SystemClock`, `SystemEntropy` | `ManualClock`, `SeededEntropy` | M1 |
| `Telemetry` / `SpanHandle` | `observability.ports` | `OtelTelemetry` | `RecordingTelemetry` | M1 |
| `EventSink` | `observability.ports` | `BufferedEventSink`, `SampledEventSink`, `LogEventSink` | `MemoryEventSink` | M1 |
| `EventWriter`, `TraceQuery` | `observability.ports` | `PostgresEventStore` | `MemoryEventStore` | M1 |
| `AuditStore` | `observability.ports` | `PostgresAuditStore` | `MemoryAuditStore` | M1 |
| `PayloadStore` | `observability.ports` | *pending storage and secret-manager decisions (§15)* | `MemoryPayloadStore` | M1 |
| `Diagnostics` | `observability.ports` | `LogDiagnostics` | `MemoryDiagnostics` | M1 |
| `TokenVerifier` | `access.ports` | `JwtTokenVerifier` (JWKS) | `StaticTokenVerifier` | M2 |
| `SecretManager` | `access.ports` | `EnvSecretManager` (local dev; production pending §15) | `SentinelSecretManager` | M2 |
| `IdempotencyStore` | `idempotency.ports` | `PostgresIdempotencyStore` | `MemoryIdempotencyStore` | M3 |
| `OwnerAlerts` | `idempotency.ports` | `LogOwnerAlerts` (interim; channel TBD) | `MemoryOwnerAlerts` | M3 |
| `Reconciler` | `idempotency.ports` | *none in Phase 1: people resolve `UNKNOWN`* | — | M3 |
| `AgentHarness` | `golden.ports` | *none until M5b* | `ScriptedAgent` | M5 |
| `Judge` | `golden.ports` | *none until M5b* | `ScriptedJudge` | M5 |
| `SandboxFactory` / `SandboxSession` | `golden.ports` | `GoldenSandbox` (wired in `composition`) | — | M5 |
| `EgressGuard` | `golden.ports` | `SocketEgressGuard` (tripwire) | `RecordingEgressGuard` | M5 |
| `Next`, `Stage` | `adapter_kernel.pipeline` | `AccessStage`, `IdempotencyStage`; order fixed in `composition.stage_order` | `FaultyConnector` (stub backend, every delivery outcome) | M0 |

Contract suites that every adapter of a port must pass live in `tests/contracts.py`.

## 11. CLI reference

<!-- BEGIN GENERATED: cli -->
Exit codes: `0` success, `1` check failed or nothing found, `3` configuration or tool error.

#### `adapter-verify access explain`

Print the decision for one agent, tool and version, and the rule that made it.

| Option | Type | Default | Required | Meaning |
| --- | --- | --- | --- | --- |
| `--agent` | `text` | — | yes | Agent ID. |
| `--tool` | `text` | — | yes | Tool name. |
| `--version` | `text` | — | no | Tool version; default version if omitted. |

#### `adapter-verify audit anchor`

Record the head of every chain that changed since its last anchor. Run on a schedule.

| Option | Type | Default | Required | Meaning |
| --- | --- | --- | --- | --- |
| — | | | | no options |

#### `adapter-verify audit verify`

Verify every chain and every anchor. Exit 1 if anything was altered.

| Option | Type | Default | Required | Meaning |
| --- | --- | --- | --- | --- |
| — | | | | no options |

#### `adapter-verify baseline accept`

Write a new baseline for review. Commit it in a PR; a CODEOWNER approves.

| Option | Type | Default | Required | Meaning |
| --- | --- | --- | --- | --- |
| `--source` | `text` | — | yes |  |

#### `adapter-verify baseline extract`

Print the shape of a source's current samples.

| Option | Type | Default | Required | Meaning |
| --- | --- | --- | --- | --- |
| `--source` | `text` | — | yes |  |

#### `adapter-verify contract check`

Run all four checks. Exit 0 pass, 1 blocked, 2 review required, 3 unknown or tool error.

| Option | Type | Default | Required | Meaning |
| --- | --- | --- | --- | --- |
| `--review-approved` | `boolean` | — | no | The PR carries contract-review-approved. |
| `--report` | `file` | — | no | Write Markdown here. |

#### `adapter-verify contract diff`

Diff two shape files and classify every change.

| Option | Type | Default | Required | Meaning |
| --- | --- | --- | --- | --- |
| `--base` | `file` | — | yes |  |
| `--rev` | `file` | — | yes |  |
| `--check` | `choice` | `upstream` | no |  |

#### `adapter-verify contract release`

Record a contract version as released (commit the lock file in a reviewed PR).

| Option | Type | Default | Required | Meaning |
| --- | --- | --- | --- | --- |
| `--tool` | `text` | — | yes |  |
| `--version` | `text` | — | yes |  |

#### `adapter-verify db migrate`

Apply pending forward-only migrations.

| Option | Type | Default | Required | Meaning |
| --- | --- | --- | --- | --- |
| `--migrations-dir` | `directory` | `migrations` | no | Directory of NNNN_name.sql files. |

#### `adapter-verify golden gate`

Promotion gate (brief 6.9). Exit 0 pass, 1 fail or stale, 2 blocked by quarantine.

| Option | Type | Default | Required | Meaning |
| --- | --- | --- | --- | --- |
| `--tool` | `text` | — | yes | Tool being promoted. |
| `--version` | `text` | — | yes | Version being promoted. |
| `--results` | `file` | — | yes | Results of the suite run to judge the promotion on. |

#### `adapter-verify golden lint`

Check task files, agent configs, tool definitions and fixtures. Exit 1 on findings.

| Option | Type | Default | Required | Meaning |
| --- | --- | --- | --- | --- |
| — | | | | no options |

#### `adapter-verify golden run`

Run golden tasks. Exit 0 all pass, 1 any fail, 2 incomplete (not judged), 3 tool error.

| Option | Type | Default | Required | Meaning |
| --- | --- | --- | --- | --- |
| `--task` | `text` | — | no | Run this task (repeatable). |
| `--agent` | `text` | — | no | Run this agent's tasks (repeatable). |
| `--tool` | `text` | — | no | Run tasks touching this tool (repeatable). |
| `--all` | `boolean` | — | no | Run every task. |
| `--changed-since` | `text` | — | no | Run tasks affected since this ref. |
| `--previous` | `file` | — | no | Earlier results, for quarantine recommendations. |
| `--results` | `file` | — | no | Results file. |
| `--report` | `file` | — | no | Markdown report. |

#### `adapter-verify golden select`

Print the tasks a change affects, with the reason (brief 6.7).

| Option | Type | Default | Required | Meaning |
| --- | --- | --- | --- | --- |
| `--changed-since` | `text` | — | yes | Git ref to compare against. |

#### `adapter-verify idem purge`

Delete expired COMPLETED and FAILED_RETRYABLE rows. UNKNOWN rows are never deleted.

| Option | Type | Default | Required | Meaning |
| --- | --- | --- | --- | --- |
| `--limit` | `integer range` | `10000` | no |  |

#### `adapter-verify idem resolve`

Settle one UNKNOWN record by hand. Exit 1 if no UNKNOWN record has this key.

| Option | Type | Default | Required | Meaning |
| --- | --- | --- | --- | --- |
| `--agent` | `text` | — | yes | Agent ID of the record. |
| `--tool` | `text` | — | yes | Tool name of the record. |
| `--key` | `text` | — | yes | Idempotency key of the record. |
| `--as` | `choice` | — | yes | completed: the effect happened. failed: it did not; the key becomes reusable. |
| `--reason` | `text` | — | yes | Why; recorded on the audit trail. |
| `--operator` | `text` | — | yes | Operator ID; the OS user is recorded too. |

#### `adapter-verify idem sweep`

Mark RESERVED rows whose lease expired as UNKNOWN, audit and alert. Run on a schedule.

| Option | Type | Default | Required | Meaning |
| --- | --- | --- | --- | --- |
| `--limit` | `integer range` | `1000` | no |  |

#### `adapter-verify idem unknown`

List rows awaiting reconciliation, oldest first.

| Option | Type | Default | Required | Meaning |
| --- | --- | --- | --- | --- |
| `--limit` | `integer range` | `1000` | no |  |

#### `adapter-verify policy lint`

Check every policy file against the lint rules and the tool catalog. Exit 1 on findings.

| Option | Type | Default | Required | Meaning |
| --- | --- | --- | --- | --- |
| — | | | | no options |

#### `adapter-verify rules list`

Print every rule with its classifications.

| Option | Type | Default | Required | Meaning |
| --- | --- | --- | --- | --- |
| — | | | | no options |

#### `adapter-verify trace`

Print every recorded step for one correlation ID, oldest first (brief 8.9).

| Option | Type | Default | Required | Meaning |
| --- | --- | --- | --- | --- |
| `--correlation-id` | `text` | — | yes | Correlation ID to reconstruct. |
| `--format` | `choice` | `table` | no |  |
<!-- END GENERATED -->

## 12. Database tables

Migrations are forward-only files in `migrations/`, applied by `adapter-verify db migrate`. The runner records each file's SHA-256 in `schema_migrations` and refuses to run if an applied file changed.

#### `call_events` (0001)

| Column | Type | Meaning |
| --- | --- | --- |
| `id` | `bigserial` PK | Insertion order; tie-breaker for equal timestamps. |
| `correlation_id` | `text` | Join key; indexed with `occurred_at, id`. |
| `occurred_at` | `timestamptz` | Event timestamp. |
| `schema_version` | `integer` | `CallEvent` schema version. |
| `event` | `jsonb` | The full `CallEvent`. Never contains bodies. |

#### `audit_log` (0002)

| Column | Type | Meaning |
| --- | --- | --- |
| `chain_id` | `text` | Chain (principal); PK with `seq`. |
| `seq` | `bigint` | Position in chain, from 1, gapless. |
| `prev_hash` | `text` | Previous record's hash; null exactly when `seq = 1`. |
| `record_hash` | `text` | SHA-256 hex over chain, seq, prev_hash and event. |
| `recorded_at` | `timestamptz` | Database insert time. Not covered by the hash; `event.occurred_at` is. |
| `event` | `jsonb` | `AuditEvent`, with details already redacted. |

#### `idempotency_records` (0003)

| Column | Type | Meaning |
| --- | --- | --- |
| `agent_id`, `tool`, `idem_key` | `text` PK | Key scope (§7.2). `idem_key` is checked against the key charset. |
| `fingerprint` | `bytea` | SHA-256 of the JCS arguments; 32 bytes. |
| `state` | `text` | `RESERVED`, `COMPLETED`, `FAILED_RETRYABLE` or `UNKNOWN`. |
| `semantic_version` | `text` | Tool version of the attempt; a different version conflicts. |
| `attempt_id` | `uuid` | Fencing token; rotated on every re-reservation. |
| `correlation_id` | `text` | The attempt's correlation ID. |
| `result_ref` | `text` | Payload-store reference of the stored result. Never the body. |
| `error_code` | `text` | Error recorded after an acknowledged effect. |
| `lease_expires_at` | `timestamptz` | Required while `RESERVED`; cleared on settlement. |
| `expires_at` | `timestamptz` | Retention; `idem purge` deletes after it (never `UNKNOWN`). |
| `created_at`, `updated_at` | `timestamptz` | Adapter clock. |

Indexes: `idem_open_states (state, lease_expires_at)` for `RESERVED`/`UNKNOWN`, and `idem_expiry (expires_at)`.
