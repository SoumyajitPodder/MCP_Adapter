# DESIGN.md — decision log (adapter_verify, LLD §5–9)

Reverse-chronological. One section per working session. Never rewrite history. If something here is wrong, add a correction entry that says what was wrong.

**Status tags:** `approved by owner` · `pending` (awaiting owner) · `pending-romik` (needs Romik and owner) · `rejected`.
**IDs:** decisions `D-NNN`, questions `Q-NNN`, risks `R-NNN`. IDs are never reused.

---

## 2026-09-25 — Session 14: M3 implemented

The owner gave the go-ahead for M3 (D-059). Built per the Session 6 design and D-037:
- the `idempotency` component:
  - pure key rules and RFC 8785 fingerprints (`rfc8785`, approved in Session 7), `decide()` for the §7.5 table and `settle()` for delivery outcomes (R-002–R-004);
  - `IdempotencyStage`, second in `composition.stage_order`, and `IdempotencyMaintenance` (sweep, purge, unknown, resolve);
  - ports `IdempotencyStore`, `OwnerAlerts`, `Reconciler` (no Phase 1 adapter);
  - `PostgresIdempotencyStore`, `LogOwnerAlerts`, in-package fakes, and the fault-injecting stub backend `FaultyConnector` (X-6);
- migration `0003_idempotency_records` (§7.7 plus `attempt_id`, `semantic_version`);
- CLI `idem sweep|purge|unknown|resolve`; settings `ADAPTER_IDEMPOTENCY_*`;
- the entry now logs an idempotency key only if it passes the §7.2 rules.

Verified:
- 448 unit/property tests (95.6% coverage) and 16 Postgres integration tests.
- §7.10 on real Postgres:
  - 50 concurrent identical calls → one upstream call;
  - a subprocess killed mid-call → sweeper `UNKNOWN` → retry `RECONCILIATION_PENDING` → `idem resolve`;
  - key reuse with other arguments → `IDEMPOTENCY_KEY_CONFLICT`.
- A Hypothesis state machine over calls, faults, time, sweeps and resolutions: at most one effect per key; replays equal the first result. It fails when `SENT_NO_RESPONSE` is mutated to retryable.
- Only synthetic mutating tools are used (in test catalogs); the pilot catalog is unchanged.

| ID | Decision | Why / rejected | Status |
| --- | --- | --- | --- |
| D-059 | Go-ahead to implement M3 | — | approved by owner |
| D-060 | Same key and arguments with a different tool version → `IDEMPOTENCY_KEY_CONFLICT` | A stored result is only replayed in the shape it was produced in. Rejected: fingerprinting the version (hides why it conflicts). | pending |
| D-061 | An `ACKED` failure whose code invites a retry is stored and returned as `INTERNAL` | Replays never change, so "retry" would mislead. `AFTER_DELAY` codes also can't be replayed without a delay. | pending |
| D-062 | A success always completes, even without an `ACKED` status; an alert flags the connector bug | A success proves the backend answered. Rejected: `UNKNOWN`, which hides a result the adapter has. | pending |
| D-063 | A `RESERVED` row whose lease expired answers `RECONCILIATION_PENDING` before the sweeper runs | Never freed and never "in progress" once the lease is gone (§7.4) | pending |
| D-064 | New span `idempotency.settle` after `backend.call` | Admission and outcome are separate steps; the §8.2 tree has one span for both | pending |
| D-065 | Lease and expiry use the adapter's `Clock`, not the database `now()` | Deterministic tests with no sleeps (X-1). Needs synchronized instance clocks. | pending |
| D-066 | A row resolved as `completed` by a person replays an empty success with warning `idempotency:resolved-manually` | The effect happened and no result exists. Rejected: `INTERNAL` (invites a new key, so a duplicate). A new error code would be a kernel change. | pending |
| D-067 | If the result can't be stored after an `ACKED` success, the row becomes `UNKNOWN` | `COMPLETED` without a replayable result would need D-066's workaround for a machine failure | pending |
| D-068 | If the settlement can't be written, the agent still gets the real outcome; the row expires into `UNKNOWN` with an alert | The effect already happened. Hiding its result helps nobody. | pending |
| D-069 | Losing the fence (sweeper took the row) gives the agent `RECONCILIATION_PENDING`, except for an `ACKED` completion (R-005) | The sweeper's `UNKNOWN` and its alert already stand | pending |
| D-070 | `idem resolve` restarts retention; `idem sweep` audits every row and exits 3 after the batch if any audit write failed | A resolved key must outlive the agent's retries; a partial audit must be visible | pending |
| D-071 | `OwnerAlerts` port with a structured-log adapter until an alert channel is chosen | §7.8 needs an owner alert; the channel is not decided | pending |

Not done: R-017 payload pinning for `UNKNOWN` rows waits on the payload-store adapter (§15). The startup check that each lease exceeds its connector timeout waits on Romik's connector configuration. Input validation doesn't exist yet, so fingerprints cover the arguments as received.

---

## 2026-09-25 — Session 13: M4 implemented

Built on `feat/m4-contract-ci`:
- the `contract_ci` component: rule book (27 rules as data), differ with rename detection, `observed`/`file` extractors, canonical contracts and release lock, the four checks, gate, report;
- the file repository, the CLI (`contract check|diff|release`, `baseline extract|accept`, `rules list`) and a CI `contract-check` job;
- pilot data: 3 sources, 3 contracts, synthetic samples, accepted baselines, released lock.

Verified: 362 unit/property tests (96% coverage), including §5.11 acceptance. REST field removal → blocked with `OUTPUT_FIELD_REMOVED`, report names `order-status-agent`. CSV column removal → blocked. Delimiter change → UNKNOWN. New enum value or date-format flip → review. Released-contract edit → blocked. Hand-edited baseline → rejected.

| ID | Decision | Why / rejected | Status |
| --- | --- | --- | --- |
| D-053 | `SourceKind.CANONICAL` added to the kernel draft | Contracts diff through the same `Shape` model. Kernel change; noted for Romik. | pending-romik |
| D-054 | Released contracts are pinned by a lock file (`contracts/released.json`), not by git history | CI needs no base-branch checkout or token; `contract release` is an explicit, reviewed step | pending |
| D-055 | Mapping replay passes when `mappings/` is empty and fails closed otherwise | A permanently red CI would be ignored; there is nothing to replay until mappings exist | pending |
| D-056 | Rename similarity compares whole normalized paths (SequenceMatcher), not leaf names | Leaf-only matching paired `customer.id` with `client.id` (found by the acceptance test) | pending |
| D-057 | No enum inference from samples; enum fields are declared per source (`enum_paths`, CSV layout) | Guessing which strings are enums would make value drift invisible or noisy | pending |
| D-058 | `FORMAT_CHANGED` is REVIEW upstream but BREAKING canonical; renames are BREAKING canonical | Agents can't re-parse or re-map. The adapter can. | pending |

---

## 2026-09-25 — Session 12: M4 approved

| ID | Decision | Status |
| --- | --- | --- |
| D-050 | M4 design and M4-Q1–Q4 approved as recommended | approved by owner |
| D-051 | D-041–D-049 approved | approved by owner |
| D-052 | First push: our work squashed onto `shaswat_changes` ("Initial setup"), code at the repo root, no local-history artifacts | approved by owner |

---

## 2026-09-25 — Session 11: M4 (contract CI, §5) design proposal — approved (D-050)

**Pilot reality (R-027):** two REST backends with no spec, plus one CSV feed. So M4 starts with the `observed` and `file` extractors. OpenAPI and oasdiff wait until a real spec exists, so there's no new dependency.

| Part | Design |
| --- | --- |
| Package | `adapter_verify/contract_ci/`: `domain/` (differ, rules, gate, report), `extractors/`, `service.py` |
| Extractors | **`observed`**: JSON samples → `Shape`. Required = present in every sample; nullable = any null. Provenance is `INFERRED` below 200 samples spanning 7 days, else `OBSERVED`. **`file`**: a declared CSV layout (header, column types) gives `DECLARED`; CSV samples give `OBSERVED`. Registry keyed by `SourceKind`. |
| Rules as data | `contract_ci/rules.yaml`: `id`, `direction`, `applies_to: [upstream, canonical]`, `classification`, `rationale`. Brief §5.5 set **+** R-009 gaps (operation added/removed, error set, format, input optional/required, enum added/removed, nullability) **+** a catch-all `UNCLASSIFIED_CHANGE → UNKNOWN`, which makes "empty diff ⇔ equal hash" hold by construction. |
| Differ | Pure `diff(base, rev, ctx) -> list[Finding(rule_id, operation, path, classification, detail)]`, then `worst()` gives the verdict. If either side is `INFERRED`, a COMPATIBLE result from a presence rule becomes REVIEW_REQUIRED. Property tests: `diff(a,a)=[]`; empty ⇔ hashes equal. |
| Romik alignment (R-024) | Each of his detector event types maps to one rule ID (e.g. `FIELD_REMOVED → OUTPUT_FIELD_REMOVED`, `ENUM_NEW → OUTPUT_ENUM_VALUE_ADDED`, `DATE_FORMAT_CHANGED → FORMAT_CHANGED`, `ENVELOPE_ADDED → ENVELOPE_CHANGED`). His rename score (0.5 name + 0.2 type + 0.3 value) drives `FIELD_RENAMED_SUSPECTED` only. `FIELD_RENAMED_KNOWN` needs an alias in the mapping registry. |
| Mapping-aware upstream diff (R-010) | A `MappingIndex` port answers "which upstream paths does any mapping read". Until Romik's mapping format exists, there's no adapter, and every upstream removal classifies at full severity (fail closed). |
| Four checks | ① **Upstream diff:** baseline vs new samples/layout. ② **Canonical diff:** `contracts/<tool>/<version>.yaml`; editing a released version fails, and a breaking change needs a MAJOR bump plus a new file. ③ **Mapping replay:** a `Translator` port; it has no adapter until Romik's engine exists, so the check reports "not run" and fails closed. ④ **Orphans:** catalog ↔ policies ↔ contracts ↔ credentials. |
| Gate | Exit codes `0` pass / `1` blocked / `2` review required / `3` unknown or tool error. REVIEW_REQUIRED passes only with the PR label `contract-review-approved`. Branch protection enforces CODEOWNER review. The override is recorded in the committed report, not in the production audit store (R-014). |
| Impact report | Findings joined with policies: who can call each affected `tool@version` (`visible_tools`). Written to `$GITHUB_STEP_SUMMARY`, which needs no extra token permissions. A PR comment or SARIF upload is a later option. |
| Baselines | `baselines/<source_id>/<source_version>.shape.json` plus content hash. `baseline accept` writes files for PR review. CI never updates them. |
| CLI | `contract check`, `contract diff --base --rev`, `baseline extract --source`, `baseline accept --source`, `rules list` (also feeds SPEC) |
| Acceptance (§5.11) | A test PR with a breaking change in each pilot source kind (REST-observed, CSV) is blocked with the right rule ID, and the report names the right agents |

**Open questions**

| ID | Question | Recommendation |
| --- | --- | --- |
| M4-Q1 | Confirm the INFERRED → OBSERVED threshold (200 samples, ≥7 days) | Yes, as a per-source setting |
| M4-Q2 | Canonical contract file format: adopt Romik's field list (`name`, `type`, `values`) now? | Yes, behind a model we can migrate; raise with Romik (R-020) |
| M4-Q3 | Mapping replay and mapping-aware diff wait on Romik's engine and format. Ship M4 with those two parts failing closed? | Yes |
| M4-Q4 | Where do samples come from before real traffic? | Synthetic recorded fixtures per source under `fixtures/samples/`, clearly labeled |

---

## 2026-09-25 — Session 10: M2 implemented

Built on `feat/m2-access`:
- the `access` component: SemVer ranges, policies and catalog, `evaluate`/`visible_tools`, lint (9 rules), credential names;
- JWT/JWKS verifier, YAML loaders, env secret manager, fakes;
- `Authenticator`, `AccessStage`, `PolicyRegistry`, `CredentialScoper`, the pipeline chain;
- CLI `policy lint` and `access explain`, and a CI `policy-lint` job.

Verified: 268 unit/property tests (94% coverage) and 11 integration tests, including §9.10 acceptance with real JWTs and Postgres audit.

| ID | Decision | Why / rejected | Status |
| --- | --- | --- | --- |
| D-041 | `InboundCall.credential` carries the bearer token. `Downstream` takes it as a third argument. It is never part of `RequestContext`. | Keeps the token out of every model that gets logged or hashed. Rejected: a field on the kernel context. | pending |
| D-042 | New span `access.authenticate`, before `access.check` | Separates token and version resolution from policy evaluation in traces | pending |
| D-043 | JWT `sub` = agent ID; `iss`, `aud`, `exp`, `iat`, `sub` required | Standard claims. `TODO(owner)`: confirm with the IdP. | pending |
| D-044 | Unknown kids refetch the JWKS at most every 30 s. A fetch failure keeps the old set only until its TTL. | Stops a random-kid flood from hammering the IdP; never uses stale keys | pending |
| D-045 | Unknown tool → `NOT_AUTHORIZED` (rule `ACCESS_UNKNOWN_TOOL`) | Tool names aren't enumerable by probing | pending |
| D-046 | Unauthenticated denials are audited on chain `system:unauthenticated` | There's no principal. This is a hot chain under attack; acceptable for Phase 1. | pending |
| D-047 | Synthetic catalog `catalog/tools.yaml` holds Romik's three read-only tools, with exactly one default version per tool | Registry stand-in (R-020). Replace when the registry exists. | pending |
| D-048 | YAML is parsed with `safe_load`, then validated as JSON | Strict models accept enum strings; YAML dates and tags are rejected | pending |
| D-049 | Dev dependency `types-pyyaml` | Type stubs for the approved `pyyaml` | pending |

---

## 2026-09-25 — Session 9: M2 approved, milestone order changed

| ID | Decision | Status |
| --- | --- | --- |
| D-037 | Accepted as recommended: M2-Q1 (JWT/JWKS behind `TokenVerifier`), M2-Q3 (audit denials plus allows on state-changing tools), M2-Q4 (grants never match pre-releases), M2-Q5 (60 s skew), M2-Q6 (`credentials.yaml`, names only, CODEOWNERS), M3-Q1–Q4 | approved by owner |
| D-038 | Milestone order is now **M2 → M4 → M3** (R-021). M4 still needs design approval. | approved by owner |
| D-039 | Pushes go only to `SoumyajitPodder/MCP_Adapter` branch `shaswat_changes` | approved by owner |
| D-040 | The orientation doc is `CONTRIBUTING.md`, replacing the brief's §14 orientation file. Docs stay lean. | approved by owner |

Still pending: M1-Q1, M1-Q6 / D-025, and D-008–D-036.

---

## 2026-09-25 — Session 8: shared repo and Romik's branches reviewed

- **Remote:** `github.com/SoumyajitPodder/MCP_Adapter`. **All of our commits go only to `shaswat_changes`** unless the owner says otherwise (owner rule). `main` and `shaswat_changes` currently hold only an empty `Readme.txt` ("Initial setup").
- **`romikChanges`** (2026-09-18): a Python/Flask "MCP Operations Sandbox" with this chain:
  - `Agent` (keyword matching) → `Gateway` (pass-through) → `MCPServer` (an in-process dict registry, not the MCP protocol) → `ATTApiDriver` (requests) → a mock Flask backend.
  - The mock backend has drift modes `rename|delay|error`.
  - Everything is unversioned and untested. Committed `__pycache__` shows **CPython 3.14**.
- **`Romik-lifecycle`** (2026-09-22 → 24): a browser-only **JavaScript** prototype of the lifecycle controller, with no backend and no tests.
  - Canonical contracts for `order.get@1.2.0`, `service.get@1.0.0`, `inventory.snapshot@1.0.0`.
  - Pure detect → classify → translate/validate modules.
  - A six-stage sanity pipeline: version check → schema comparison → adapter compatibility → smoke test → canonical validation plus last-known-good regression → readiness.
  - Review workflow, adapter states `tested → canary → primary → deprecated → retired` (plus `rolled_back`), contract states `ACTIVE/DEPRECATED/SUNSET`, and a fail-closed live-call path.
- **Impact on this half:** recorded as R-019 to R-028 below. Each is `pending` until the owner and Romik discuss.

| ID | Finding | Impact / proposal |
| --- | --- | --- |
| R-019 | Romik's production language is unclear: a Python 3.14 sandbox and a JS lifecycle controller | The brief's "one Python version" (§2) is unresolved. On 3.14, D-002's in-house UUIDv7 goes away (stdlib). Ask. |
| R-020 | The contract format is a flat field list (`name`, `type ∈ string/enum/datetime`, `values`) plus `version`/`state`. There's no input schema, no `behavior`, no `x-sensitivity`. | §7 can't know what's mutating. §8 redaction masks every field (fail closed but useless for debugging). `RedactionPolicy.from_schema` expects JSON Schema. Needs an agreed format, or a translator. |
| R-021 | Every pilot tool is **read-only**. Romik's README defers write paths. | M3 (idempotency) has nothing to protect in the pilot. Consider building M4 before M3, or M3 against synthetic mutating stubs only. |
| R-022 | New error code `CONTRACT_SUNSET`, not in the kernel enum | Add it to `ErrorCode` (kernel change) with `retry=NEVER`, or map it to an existing code |
| R-023 | His `_meta` has `contract`, `served_by`, `absorbed`, `warnings`, and no `correlation_id` | Merge into `ResponseMeta`: our `adapter_version` ≈ his `served_by`; add `contract`. |
| R-024 | His detector and classifier are already **mapping-aware** (a removed unmapped field is COMPATIBLE), with 10 event types and rename scoring | This is the candidate "one classifier" for §3 and §5 (brief §3.1, R-010). §5 rule IDs should map 1:1 to his event types. It needs porting out of JS. |
| R-025 | His lifecycle has no `draft` state. Promotion is gated by his readiness check. | §6.9 gates hook into `promote()`: tested → canary also requires golden-task pass. The boundary to agree: his LKG regression covers mapping output, ours covers agent behavior. |
| R-026 | His sandbox gateway claims correlation-ID stamping, scoped credentials and auth as future gateway work. His sandbox roadmap also lists contract testing and golden tasks. | Ownership overlap with §5, §6, §8, §9. Needs one explicit split, per the brief's §1.2 table. |
| R-027 | Pilot backends are now concrete (still synthetic): two REST backends (`order-management`, `service-inventory`) and one FILE feed (`inventory_YYYYMMDD.csv`) | Answers part of §15. M4 needs `observed` and `file` extractors first, not OpenAPI. |
| R-028 | His drift injectors (rename, enum add, date format, envelope, column reorder, delimiter, sunset) | Reuse them as §6.8 mutation canaries and §5 rule fixtures |

---

## 2026-09-22 — Session 7: M2/M3 kickoff, dependency approval

- The owner asked to work on M2 and M3, and **approved `pyyaml`, `pyjwt[crypto]` and `rfc8785`**. That settles M2-Q2 and M3-Q5 (approved by owner).
- The owner chose to **review the remaining recommendations item by item** before implementation: M1-Q6, M2-Q1/Q3–Q6, M3-Q1–Q4, D-008–D-036. Implementation is on hold until those answers arrive.

---

## 2026-09-22 — Session 6: M2 (access control) and M3 (duplicate prevention) design proposals

Status of this whole section: **`pending`**. Nothing here is implemented. Both milestones need owner approval, and M2 also needs answers to M2-Q1–Q6.

### M2 · Baseline access control (§9)

**Package:** `adapter_verify/access/`, with the usual `domain/`, `ports.py`, `adapters/`, `service.py`.

**1. Policy model** (`domain/policy.py`)
- `Policy(agent_id, owner, scope: read|write, grants: tuple[Grant])` and `Grant(tool, versions: VersionRange)`.
- Frozen, strict, closed. Loaded from `policies/*.yaml`, one agent per file, file name = `agent_id`.
- **Version ranges** use an in-house, pure SemVer 2.0 core: `MAJOR.MINOR.PATCH`, comparators `>= > <= < ==`, comma-joined.
  - Pre-release versions are **never** matched by a grant. That's fail closed, pending Romik's semver table.
  - Rejected: `packaging.specifiers`, because PEP 440 isn't SemVer (pre-release and local-version semantics differ). Also rejected: the `semver` package, one more dependency for about 60 lines of code.
- **"A MAJOR bump never inherits access" (§9.2) is enforced structurally.** Every range must sit inside one MAJOR: its lower bound is at least `N.0.0` and its upper bound is at most `<N+1.0.0`. A range like `>=1.0.0` alone is a lint error.

**2. Evaluation** (`domain/evaluate.py`)
- A pure function, `evaluate(identity, tool, version, behavior, index) -> Decision(allowed, rule_id, error_code, reason)`, over an in-memory `PolicyIndex` (dict by agent, dict by tool).
- Check order, following §9.6:

| # | Check | Rule ID | Error |
| --- | --- | --- | --- |
| 1 | No policy for agent | `ACCESS_NO_POLICY` | `NOT_AUTHORIZED` |
| 2 | Tool not granted | `ACCESS_TOOL_NOT_GRANTED` | `NOT_AUTHORIZED` |
| 3 | Version outside every grant range | `ACCESS_VERSION_OUT_OF_RANGE` | `VERSION_NOT_PERMITTED` |
| 4 | Mutating or destructive tool with `read` scope | `ACCESS_SCOPE_READ_ONLY` | `SCOPE_VIOLATION` |
| — | Allowed | `ACCESS_GRANTED:<agent>/<tool>/<grant-index>` | — |

- Token failures happen before evaluation: `ACCESS_TOKEN_INVALID` → `NOT_AUTHORIZED`.
- **Proposed addition (X-3):** every decision carries the `policy_set_hash` (SHA-256 of the canonical, sorted policy set), so any past decision can be replayed exactly.

**3. Identity** (`ports.TokenVerifier`)
- `verify(token: SecretStr) -> VerifiedIdentity(agent_id, issuer, audience, expires_at)`.
- On any failure it raises `TokenRejected`, which carries no detail.
- Checks: signature, `iss`, `aud`, `exp`, `nbf`, and clock skew of at most `ADAPTER_ACCESS_CLOCK_SKEW_S` (default 60, max 300).
- **Proposed JWT adapter** (the IdP is still unknown, §15):
  - Algorithm allow-list: `RS256`, `ES256`, `EdDSA`. `none` and `HS*` are rejected.
  - JWKS fetched with a TTL cache. If the fetch fails and there's no fresh cache, every token is rejected (fail closed).
- Until the IdP is known, M2 ships the port, a fake, and the JWT adapter, if a library is approved (M2-Q2).

**4. Authentication step** (R-008)
- `Authenticator.authenticate(RequestContext, credential) -> CallContext | Denial`.
- It runs between `ObservedEntry` and the stage chain. It resolves the version through Romik's registry port (R-006) and attaches `behavior`.
- **Blocked on transport:** the bearer token arrives with the transport (MCP Streamable HTTP `Authorization`), so `InboundCall` needs a `credential: SecretStr | None` field that the server fills in. That depends on M1-Q1.

**5. Two enforcement points** (§9.5)
- `tools/call`: `AccessStage`, the first stage after authentication. It's authoritative.
- `tools/list`: a pure `visible_tools(identity, catalog, index) -> list[ToolRef]` that the MCP list handler calls. It returns only tools *and versions* the agent could call.
- A test runs both points against every (agent, tool, version) combination in the fixture catalog and asserts they agree. Hiding is never looser than blocking.

**6. Policy lint** (§9.3, CI: `adapter-verify policy lint`)

| Rule ID | Fails when |
| --- | --- |
| `POLICY_UNKNOWN_TOOL` | the grant names a tool not in the catalog |
| `POLICY_UNKNOWN_VERSION` | the range matches no released version |
| `POLICY_WILDCARD` | a tool name contains `*`, `?`, or a regex metacharacter |
| `POLICY_CROSS_MAJOR` | the range isn't bounded inside a single MAJOR |
| `POLICY_WRITE_WITHOUT_OWNER` | scope is `write` and `owner` is empty |
| `POLICY_READ_SCOPE_MUTATING_GRANT` | a `read` agent is granted a mutating or destructive tool |
| `POLICY_DUPLICATE_AGENT` | two files declare the same `agent_id` |
| `POLICY_FILE_NAME_MISMATCH` | file name ≠ `agent_id` |

- The catalog comes from a `ToolCatalog` port (Romik's registry). Until that exists, a synthetic catalog fixture stands in (§0.5).

**7. Loading** (§9.8)
- At startup, an invalid policy set means the process refuses to start.
- Reload happens on an interval or a signal. An invalid new set keeps the last good set and raises one diagnostic per change.
- The index is swapped atomically: a single reference assignment, so readers never see half a set.

**8. Audit** (§8.7, M1-Q2)
- Every **denial** is audited with rule ID, correlation ID and `policy_set_hash`.
- **Allows** are audited for mutating and destructive tools. Read allows go to events only (M2-Q3).
- If the audit write fails on a mutating call → `INTERNAL`, and the call does not proceed (fail closed).

**9. Credentials** (§9.7)
- `SecretManager` port: `get(name) -> SecretStr`, with a short-TTL cache.
- `CredentialScoper`: `(tool, version) -> credential name`, from a reviewed mapping file `credentials.yaml`. That file holds names only, never values. The scoper returns the scoped secret for the connector.
- The fake secret manager returns sentinel values, which the §6 sandbox and the sentinel scan rely on.
- The production adapter waits on the secret-manager decision (§15).

**10. CLI**
- `adapter-verify access explain --agent A --tool T --version V`: prints the decision, rule ID and policy hash. Also used by the §5.8 impact report.
- `adapter-verify policy lint`.

**Acceptance (§9.10)**
- An ungranted agent is blocked at `tools/call` and never sees the tool in `tools/list`.
- The denial is audited with its rule ID.
- The sentinel scan finds no secret. The fake secret manager's sentinel values are exercised on every test.

**Dependencies requested**
- A YAML parser, **`pyyaml`**, used with `safe_load` only. M4 rules and M5 tasks need it too. The alternative is TOML via stdlib `tomllib`, which would deviate from the brief's YAML examples.
- A JWT library, **`pyjwt[crypto]`**. Rejected: `python-jose`, which is unmaintained. `joserfc` is the alternative.

**Open questions for M2**

| ID | Question | Recommendation |
| --- | --- | --- |
| M2-Q1 | Token format and IdP: JWT with JWKS? | Assume JWT/JWKS behind the port; confirm with the IdP owner |
| M2-Q2 | Approve `pyyaml` and `pyjwt[crypto]`? | Yes |
| M2-Q3 | Audit allows for reads too, or only denials and mutating allows? | Denials and mutating allows only (volume) |
| M2-Q4 | Grants never match pre-release versions? | Yes, until Romik's semver table says otherwise |
| M2-Q5 | Clock skew default of 60 s? | Yes |
| M2-Q6 | Where does the per-tool credential mapping live: `credentials.yaml` in the repo (names only) or the tool definition? | Repo file under CODEOWNERS |

### M3 · Duplicate prevention (§7)

**Package:** `adapter_verify/idempotency/`

**1. Key rules**
- Keys match `[A-Za-z0-9._:-]{1,128}`. An invalid key is rejected with `INVALID_INPUT` (never truncated).
- A state-changing tool without a key gets `IDEMPOTENCY_KEY_REQUIRED`. There is no derived key (R-001, approved).
- Keys are scoped by `(agent_id, tool, key)`.

**2. Fingerprint**
- SHA-256 over RFC 8785 (JCS) of the *validated* arguments.
- Library: **`rfc8785`** (Trail of Bits, pure Python). JCS number serialization is subtle, so this shouldn't be hand-written.
- Property tests: key order and whitespace never change the fingerprint; any value change does.

**3. Migration 0003 `idempotency_records`**
- Brief §7.7 columns, plus:
  - `attempt_id uuid NOT NULL`: the fencing token (R-005).
  - `semantic_version text NOT NULL`: so a replay across versions can be detected.
- The brief's indexes: open states, and expiry.

**4. Stage**
- Position: after input validation, before the connector (§1.3).
- `decide(existing, fingerprint) -> Action` is a pure function implementing the §7.5 table.
- Reservation is the single atomic statement `INSERT … ON CONFLICT DO NOTHING RETURNING`. Re-reserving a `FAILED_RETRYABLE` row is a conditional `UPDATE … WHERE state = 'FAILED_RETRYABLE'` that also rotates `attempt_id`.

**5. Settlement**
`settle(delivery, outcome) -> (state, error_code, agent_result)` is pure and uses R-002/R-003/R-004:

| Delivery | New state | Agent sees |
| --- | --- | --- |
| `None` (never reached a connector) · `NOT_SENT` · `REJECTED_NO_EFFECT` | `FAILED_RETRYABLE` (key reusable) | the original failure |
| `ACKED`, success | `COMPLETED`, result stored in the payload store → `result_ref` | the result |
| `ACKED`, later failure (translation or validation) | `COMPLETED` with `error_code`, plus an owner alert | that error, identical on every replay |
| `SENT_NO_RESPONSE` | `UNKNOWN`, alert | `RECONCILIATION_PENDING` |
| Exception escaping the downstream stages | `UNKNOWN` (we can't prove nothing was sent), alert; then the exception is re-raised to `ObservedEntry` | `INTERNAL` |

- Every update is fenced by `WHERE attempt_id = $mine`. A late completion can move `UNKNOWN → COMPLETED` only with the matching `attempt_id` and only on `ACKED`, and that transition is audited (R-005).

**6. Leases and sweeper**
- `lease_expires_at = now + lease`. The lease defaults to 30 s and is configurable per tool. It must exceed the connector timeout, which is checked at startup against Romik's connector config once that exists.
- `adapter-verify idem sweep` (also runnable as a periodic task) moves expired `RESERVED` rows to `UNKNOWN`, audits the change and raises an alert.
- `DUPLICATE_IN_PROGRESS` returns `retry_after_ms` = the remaining lease, with a floor of 100 ms.

**7. Replay**
- `COMPLETED` → read `result_ref` from the payload store → return it with `_meta.replayed = true`. No upstream call.
- Payload retention is at least idempotency retention, and payloads referenced by `UNKNOWN` rows are pinned (R-017).

**8. Audit and fail closed**
- Each state transition is audited (`IDEMPOTENCY_TRANSITION`).
- The *reservation* audit is written **before** the upstream call. If it fails, the reservation is released as `FAILED_RETRYABLE` (nothing was sent) and the agent gets `INTERNAL` (M1-Q2).
- An audit failure *after* an `ACKED` effect can't undo the effect. The DB state is still written, and the failure is raised as a diagnostic alert.

**9. Retention and reconciliation**
- `adapter-verify idem purge` deletes expired `COMPLETED` and `FAILED_RETRYABLE` rows. It **never** deletes `UNKNOWN` rows. Retention defaults to 72 h, per-tool overrides are allowed, and nothing may be shorter than the longest agent retry window (§7.7).
- `adapter-verify idem unknown` lists rows awaiting reconciliation.
- `adapter-verify idem resolve --agent … --tool … --key … --as completed|failed --reason "…"`: `--reason` is required, and the resolution is audited on the operator's chain.
- A `Reconciler` port exists for automatic read-back, but has no Phase 1 adapter.

**10. Upstream passthrough** (§7.9)
- The key is available to connectors on `CallContext`, so forwarding it as `Idempotency-Key` is Romik's connector concern.
- Each backend's actual behavior goes in the backend registry (M3-Q4).

**Acceptance (§7.10) and extra tests**
- 50 concurrent identical calls on real Postgres make exactly one upstream call, counted by a stub.
- Crash test: a subprocess is killed mid-call. After the lease expires, the sweeper moves the row to `UNKNOWN`, and a retry gets `RECONCILIATION_PENDING`.
- Reusing a key with different arguments gives `IDEMPOTENCY_KEY_CONFLICT`.
- Fault-injecting stub connector (X-6) that produces every `DeliveryStatus`.
- Hypothesis state-machine test over the §7.5 table.

**Dependencies requested:** `rfc8785`.

**Open questions for M3**

| ID | Question | Recommendation |
| --- | --- | --- |
| M3-Q1 | Key format `[A-Za-z0-9._:-]{1,128}`, the same as correlation IDs? | Yes |
| M3-Q2 | Default lease 30 s and retention 72 h? Per-tool overrides in adapter config or the tool definition? | Adapter config now; move to the tool definition if Romik agrees |
| M3-Q3 | How are operators identified for `idem resolve` before operator auth exists? | `--operator` flag plus OS user, both recorded; replace with real auth later |
| M3-Q4 | Which pilot backends honour `Idempotency-Key`? | Unknown. Record per backend, never assume (§7.9) |
| M3-Q5 | Approve `rfc8785`? | Yes |

---

## 2026-09-22 — Session 5: M1 completed (delivery steps 4–6)

M1-Q6 is still unanswered, so the audit chain granularity is implemented as recommended (per principal), isolated in `chain_id_for`, and marked pending.

### What was built

4. **Audit trail**
   - Migration `0002_audit_log` (append-only trigger).
   - Pure hash-chain domain: `link`, `verify_chain`, anchors, `heads_to_anchor`, `verify_against_anchors`.
   - `AuditTrail` service (record, anchor, verify), `PostgresAuditStore` (per-chain advisory lock), and a memory fake.
5. **Events and trace**
   - Migration `0001_call_events`.
   - `BufferedEventSink`: bounded, never blocks, evicts sampled events first, requeues critical events on write failure.
   - `PostgresEventStore` (writer and trace query).
   - Pure table/JSON trace rendering.
   - The `adapter-verify` CLI: `db migrate`, `trace`, `audit anchor`, `audit verify`.
   - A forward-only, checksummed migration runner.
6. **Sentinel scan and acceptance**
   - `tests/sentinels.py`: an autouse scan of every in-memory sink, OTel exporter and log record after **every** test. The plugin has its own self-test, which proves it catches leaks.
   - `PayloadStore` port, memory fake, and the contract suite in `tests/contracts.py`.
   - §8.10 acceptance on real Postgres and the real OTel SDK.
7. **CI and docs**
   - CI `integration` job.
   - SPEC: hand-written §1, §8, §10 and §12, plus generated CLI reference, database config and audit schemas.

**Verified locally:**
- 171 unit/property tests, 95.6% coverage. The Postgres adapters and the CLI's database paths are covered by the integration job, not by the unit coverage figure.
- 10 integration tests on `postgres:17-alpine`, including:
  - 40 concurrent audit appends producing one gapless chain;
  - superuser tampering (edit, whole-chain delete) being detected;
  - a tampered record making `adapter-verify audit verify` exit 1.
- ruff, mypy (80 files), import-linter (4 contracts), and spec freshness.

### §8.10 acceptance map

| Clause | Evidence |
| --- | --- |
| One correlation ID returns every step in order | `test_one_correlation_id_returns_every_step_in_order_without_leaking`: all 8 stages, in order, one trace, one span tree |
| No unannotated field unmasked in any sink | The redaction property tests; the sentinel scan on every test; the acceptance test puts sentinels in the upstream body, finds them in the payload store, and finds them nowhere else, including the `call_events` rows |
| Tampered audit record detected | `test_tampering_by_a_privileged_user_is_detected`, `test_acceptance_tampered_audit_record_fails_verify` |

### Decisions made while implementing

| ID | Decision | Why / rejected | Status |
| --- | --- | --- | --- |
| D-025 | `chain_id_for` implements one chain per principal (`agent:`, `operator:`, `system:`). The anchor chain is `anchor:global`. | M1-Q6 unanswered; this is the recommendation, changeable in one function | pending (Q6) |
| D-026 | The audit hash covers `event.occurred_at` (app time), not `recorded_at` (DB time) | `recorded_at` is informational. Rejected: hashing the DB default, which would need a read-back round trip. | pending |
| D-027 | `audit_log` has an append-only trigger as well as the hash chain | Stops accidental or ordinary-role changes. The chain catches privileged ones. | pending |
| D-028 | `BufferedEventSink` drops sampled events and requeues critical events when a write fails. Critical events are dropped only on overflow, with one alert per episode. | §8.8. Rejected: unbounded retry queue (memory growth during an outage). | pending |
| D-029 | CLI exit codes: `0` ok, `1` check failed or nothing found, `3` config or tool error | Matches §5.10's `3` for tool errors. Config errors never echo input values, so the DSN can't leak. | pending |
| D-030 | `PayloadStore.get(ref)` has no principal parameter yet | No permission model exists. `TODO(owner)` in the port. | pending |
| D-031 | `MIGRATIONS_DIR` resolves relative to the repository, not the installed package | Deployment packaging is undecided. To revisit when the deploy story exists (see gotchas). | pending |
| D-032 | `tests/` is a Python package (`__init__.py` in each directory) | mypy otherwise refuses two `conftest.py` modules with the same name | approved (tooling) |
| D-033 | The sentinel scan runs on every test by default. `@pytest.mark.sentinel_exempt` needs a stated reason. | Makes "never in any sink" a property of the whole suite (X-5) | pending |
| D-034 | Renamed `AuditUnavailable` → `AuditUnavailableError` | ruff N818 naming rule | approved (cosmetic) |
| D-035 | Import `testcontainers.community.postgres` | The old path emits a DeprecationWarning, which `filterwarnings=error` turns into a failure | approved (tooling) |

### Deferred / not done

- **Production `PayloadStore` adapter:** waits on the storage and secret-manager decisions (§15).
- **Structured-logging bootstrap and a long-running server entry point:** depend on M1-Q1 (MCP server ownership).
- **Scheduling `audit anchor` and `idem sweep`:** a deployment concern. Documented in the README, not automated.

---

## 2026-09-22 — Session 4: M1 answers, delivery steps 1–3 implemented

### Owner answers

| ID | Answer | Status |
| --- | --- | --- |
| M1-Q1 | Owner is unsure who owns the MCP server. M1 stays transport-neutral (`InboundCall`), so nothing is blocked. Added to the Romik note as a question for both halves. | **open** |
| M1-Q2 | Audit write failure on a mutating call → **fail closed**. | approved by owner |
| M1-Q3 | Pin mirrored semconv names to a **commit SHA**. | approved by owner |
| M1-Q4 | Invalid caller-supplied correlation ID → **reject** with `INVALID_INPUT`. | approved by owner |
| M1-Q5 | All M1 dependencies approved; **asyncpg** chosen over psycopg for speed. | approved by owner |
| M1-Q6 | Audit chain granularity (per principal). | **not yet answered**. Blocks delivery step 4 only. |
| D-008–D-014 | Not yet confirmed. | pending |

Treated as approval of the M1 design, with Q1 and Q6 open.

### Semconv pin, verified at the source

Pinned `open-telemetry/semantic-conventions-genai@8ffdf568e1b4391a99adb081db16e8102e36918e` (HEAD on 2026-09-22). Verified directly in `model/*.yaml` at that commit:

- `gen_ai.operation.name` (value `execute_tool`), `gen_ai.tool.name`, `gen_ai.tool.call.id`, `gen_ai.tool.type`.
- `mcp.method.name` (value `tools/call` for member `tools_call`).
- Every one of these is `stability: development`.

We emit `gen_ai.operation.name`, `gen_ai.tool.name` and `mcp.method.name` on `tool.call` only.
- `gen_ai.tool.call.arguments` / `.result` exist upstream, but they carry payload bodies, so they are **never emitted**.
- `gen_ai.tool.call.id` isn't emitted either: MCP gives us no call ID, and the correlation ID is a different concept.

### What was built (branch `feat/m1-observability`)

1. **IDs and context:**
   - `Clock`/`Entropy` ports, with system adapters and fakes (`ManualClock`, `SeededEntropy`).
   - Correlation-ID validation (`[A-Za-z0-9._:-]{1,128}`).
   - Pure `uuid7_from`, property-tested for layout, round-trip and time ordering.
   - The `ContextVar` with scoped `bound()`, tested for isolation across 20 concurrent tasks.
2. **Pure domain:**
   - The closed `SpanName`/`SpanAttributes` set and the `otel_attributes` mirror.
   - `CallEvent` v1.
   - `must_keep`/`should_keep` sampling.
   - `RedactionPolicy`.
   - `safe_exception_summary`: exception type and code locations only, never the message.
3. **Telemetry and entry:**
   - The `Telemetry`/`EventSink`/`Diagnostics` ports, and the OTel adapter (exception recording off).
   - In-memory fakes.
   - `ObservedEntry`, `SampledEventSink`, `ObservabilitySettings`, and a `composition.build_runtime` that wires them together.
   - The event sink is currently structured JSON through stdlib logging. The Postgres event store replaces it in step 5.

Result: 127 tests, 100% line and branch coverage, and 4 import-linter contracts. The new "domain purity" contract was checked to actually fail when a violation is introduced.

### Decisions made while implementing

| ID | Decision | Why / rejected | Status |
| --- | --- | --- | --- |
| D-015 | **Delivery steps reordered.** The step-1 "ObservedEntry skeleton" moved into step 3. | The entry needs the span's trace ID, so building it before the telemetry port would have meant rework. | pending (cosmetic) |
| D-016 | **Redaction drops undeclared object keys** and adds `"<redacted:unknown-keys>": n`. Declared-but-unannotated keys are kept with a masked value. | Map-shaped payloads (keyed by account number, say) put data in *keys*. Masking only values would leak those. A first version dropped declared-but-unannotated keys too; a unit test caught that, and it was fixed. | pending |
| D-017 | **Container annotations don't propagate** to children. Only a leaf's own annotation unmasks it. | Propagation would let one `public` on an object reveal fields added later. That fails open. | pending |
| D-018 | Schema walking follows only `properties`/`items`. Anything under `$ref`/`oneOf`/`allOf` stays masked. | Fails closed. Full JSON-Schema resolution waits until Romik's contract format is fixed. | pending |
| D-019 | OTel spans are created with `record_exception=False, set_status_on_exception=False`. | The SDK defaults copy the exception message into span events and the status description. That's a customer-data leak. | pending (security) |
| D-020 | `CallEvent` v1 adds `behavior` and `idempotency_state` to the §8.3 fields. | §8.6's never-sample rules need both. | pending |
| D-021 | The `tool.call` event has `agent_id=None`. Authentication happens downstream, so the agent is recorded on the `access.check` event. The trace query joins on correlation ID. | Rejected: having downstream mutate the entry's event. | pending |
| D-022 | Until M3, a caller's idempotency key appears in events only if it passes the correlation-ID charset check. | Rejected: logging an unvalidated caller string (log injection). §7.2's rules replace this check. | pending |
| D-023 | The `_meta` key prefix is `adapter/` for now (`TODO(owner)`). | The org's reverse-DNS namespace is unknown. Brief §0.5 says not to invent it. | pending |
| D-024 | `adapter_verify.common` holds cross-component ports (clock, entropy) and `FrozenModel`. | This isn't in the brief's §4 layout, but §7 leases and §9 token checks need the same clock. | pending |

### Deferred

- Step 4 (audit store) waits on M1-Q6.
- Steps 5–6: event store, trace CLI, sentinel plugin.
- Structured-logging configuration (installing `JsonFormatter` on the root logger) belongs with the process entry point. That depends on M1-Q1.

---

## 2026-09-22 — Session 3: M1 (observability, §8) design proposal

Status of this whole section: **`pending`**. Implementation starts only after owner approval.

### Semconv re-check (brief §8.5 asked for it before M1)

Verified on 2026-09-22 with web sources (opentelemetry.io, the `open-telemetry/semantic-conventions-genai` repo, and a July 2026 status write-up):

- In core semconv v1.42.0 (June 2026), the GenAI conventions **moved to a separate repo, `semantic-conventions-genai`**. The **MCP conventions moved with them**.
- Every `gen_ai.*` attribute is still at **Development** stability.
- The new repo has **no tagged releases**, so there is no versioned schema URL to pin.

**Consequence (M1-Q3 below):** the brief's plan still holds (source-of-truth `adapter.*` attributes, with `gen_ai.*` mirrored alongside), but "pin the semconv version" has to become "pin a commit SHA of `semantic-conventions-genai`", recorded in code as a constant. The upstream MCP conventions are also a candidate mirror. I haven't checked their attribute names yet, and I won't use them until I have.

### Scope (brief §8, acceptance §8.10)

Context propagation, the span tree, event schema v1, fail-closed redaction, a payload store port, a hash-chained audit store, and `adapter-verify trace`.

### Package layout

```
src/adapter_verify/observability/
├── domain/
│   ├── ids.py          # uuid7_from(unix_ms, rand) — pure RFC 9562 layout; correlation-ID validation
│   ├── events.py       # CallEvent v1 (schema_version: Literal[1]), Stage/Outcome enums
│   ├── attributes.py   # closed allow-list of span attributes; adapter.* -> gen_ai.* mirror table
│   ├── redaction.py    # redact(payload, sensitivity_map) -> masked copy; pure, allow-list
│   ├── sampling.py     # must_keep(event) / sample(event, ratio, draw) — pure
│   └── audit_chain.py  # AuditRecord, link(prev, record), verify(chain) -> ChainReport
├── ports.py            # Clock, IdSource, Telemetry, EventSink, TraceQuery, PayloadStore, AuditStore
├── adapters/
│   ├── otel.py         # Telemetry via opentelemetry-sdk + OTLP/HTTP exporter
│   ├── postgres_audit.py
│   ├── postgres_events.py   # EventSink + TraceQuery
│   └── memory.py       # in-memory fakes of every port (X-10), shipped in the package
├── context.py          # the one ContextVar[RequestContext] + scoped setter (R-018 exception)
└── service.py          # ObservedEntry: builds RequestContext, opens tool.call span, emits events
```

### Key designs

**1. Entry point, transport-neutral.**
`ObservedEntry.handle(InboundCall) -> ToolResult`. `InboundCall` = `(tool, arguments, meta: Mapping[str, str])`. It reads the correlation ID and idempotency key from `meta` (the `_meta` carrier, approved in M0 §5). It validates the correlation ID: `[A-Za-z0-9._:-]`, 1–128 characters. An invalid ID is rejected with `INVALID_INPUT` rather than silently replaced, because replacing it would break the caller's join key. If none is supplied, it generates a UUIDv7. Then it sets the ContextVar, opens `tool.call`, calls the rest of the pipeline, and echoes `correlation_id` in `_meta`.
**This doesn't depend on the MCP SDK.** Who owns the MCP server wiring is an open question (M1-Q1).

**2. UUIDv7 (D-002).**
Pure `uuid7_from(unix_ms: int, rand: bytes) -> UUID` sets the 48-bit timestamp and the version/variant bits. The `IdSource` port supplies clock and randomness (`secrets.token_bytes`). Property tests check: version = 7, variant = RFC 4122, the timestamp round-trips, and IDs sort by time for increasing timestamps.

**3. Typed telemetry port. No raw OTel calls outside the adapter.**
`Telemetry.span(name: SpanName, attrs: SpanAttributes)`. `SpanName` is an enum of the 8 spans in §8.2. `SpanAttributes` is a closed Pydantic model: there are no free-form attributes at all, so a payload value *cannot* become a span attribute by accident (§8.4, first bullet). The OTel adapter writes each attribute as `adapter.*` and mirrors it through the `gen_ai.*` table.
*Rejected:* using the OTel API directly in services. It's already vendor-neutral, but it accepts any attribute, which is exactly the leak we want to make impossible.

**4. Redaction, fail closed.**
`redact(value, sensitivity: Mapping[path, Sensitivity])` walks the JSON value. It keeps a leaf only when its path is annotated `public` or `internal`. Every other leaf becomes `"<redacted:pii>"`, `"<redacted:secret>"` or `"<redacted:unannotated>"`. Object keys are kept (they're schema, not data). Array paths use `[]`, matching `FieldShape.path`.
*Rejected:* a deny-list, which fails open. *Rejected for Phase 1:* keyed-hash masking (joinable pseudonyms). It needs a managed key, and the secret manager is still an open question. It's a good follow-up.

**5. Events and sampling.**
`CallEvent` v1 has exactly the §8.3 fields. `payload_ref` is a string ref, never a body. `must_keep` is true for errors, denials, mutating calls, drift events, and idempotency state changes. Sampled reads use a configured ratio, **default 1.0 (keep everything)** until load targets exist. Export uses **two bounded queues**, `critical` and `sampled`. On overflow, `sampled` drops first. If `critical` overflows, it increments a counter and logs one rate-limited alert. The request path never blocks (§8.8).

**6. Audit store: per-principal hash chains (R-011, approved).**
Postgres table `audit_log(chain_id, seq, prev_hash, record_hash, recorded_at, event jsonb)`, `PK(chain_id, seq)`.
- `chain_id` = `agent:<id>` or `operator:<id>`.
- Append holds a per-chain advisory lock (`pg_advisory_xact_lock(hashtext(chain_id))`) and inserts `seq = head+1`. Contention is limited to one principal.
- `record_hash = SHA-256(prev_hash ‖ canonical_json(record without hash))`.
- An **anchor job** periodically appends every chain's `(chain_id, seq, record_hash)` head to the `anchor:global` chain.
- `verify()` detects edits (hash mismatch), deletions in the middle (seq gap or prev mismatch), and truncation behind an anchor.
- **Known limit:** truncating the tail *since the last anchor* can't be detected. The anchor interval bounds that window.
- Audit event bodies are redacted before they're hashed.

**7. Payload store.**
The port is `put(correlation_id, kind, body) -> PayloadRef` / `get(ref, principal) -> body`. M1 ships **the port, the in-memory fake, and a contract test suite** that any adapter must pass. The production adapter waits for the storage and secret-manager decisions (§15). Building AES-GCM against an invented key source would break §0.5.

**8. Trace query and CLI.**
Events that are kept go to Postgres `call_events` (indexed on `correlation_id, timestamp`) through `EventSink`, as well as OTLP. `adapter-verify trace --correlation-id X [--format table|json]` reads through `TraceQuery`, so it doesn't depend on whichever observability backend is picked later. Payload refs are printed as refs. Resolving them waits for a permission model (TODO(owner)).
*Rejected:* querying the OTel backend. It's unknown, and sampled spans can't prove "every step".

**9. Migrations.**
Forward-only `migrations/NNNN_name.sql`, run by a small in-house runner (~100 lines). It takes an advisory lock, records a `schema_migrations(version, sha256)` table, and refuses to run if an applied file's checksum has changed.
*Rejected:* Alembic, which pulls in SQLAlchemy (a sync ORM we don't otherwise use). *Rejected:* dbmate/sqitch, which are extra binaries on every dev machine.

### Acceptance tests (§8.10 mapped)

| §8.10 clause | Test |
| --- | --- |
| One ID returns every step in order | Integration: run a synthetic call through `ObservedEntry` with fake stages that emit every §8.2 span. `trace` returns all of them in order. |
| No unannotated field unmasked in any sink | X-5 sentinel plugin: an autouse fixture plants sentinel strings in unannotated and `pii` fields, then scans every in-memory sink (spans, events, audit, logs, error bodies) after **every** test. Property test: `redact` never outputs a leaf whose path isn't public/internal. |
| Tampered audit record detected | Integration on real Postgres: edit a row, delete a middle row, and truncate behind an anchor. Each one fails `verify()`. |

### Dependencies requested for M1

| Package | Why | Alternative rejected |
| --- | --- | --- |
| `pydantic-settings` | typed config (already approved in M0) | — |
| `opentelemetry-api`, `opentelemetry-sdk` | spans | none sensible |
| `opentelemetry-exporter-otlp-proto-http` | OTLP export | `-proto-grpc`: pulls in `grpcio`, a large native wheel |
| `asyncpg` (+ `asyncpg-stubs`, dev) | async Postgres for the audit and event stores | `psycopg[binary]` 3 async: also good. asyncpg is faster and more common in asyncio services. |
| `click` | CLI | `typer` (adds rich/shellingham), `argparse` (no subcommand ergonomics, weak introspection for the generated CLI reference) |
| `testcontainers` (dev) | real Postgres in integration tests | docker-compose fixtures (manual lifecycle) |

### Open questions for M1

| ID | Question | Recommendation |
| --- | --- | --- |
| M1-Q1 | Who owns the MCP server process and SDK wiring: this half, Romik, or shared? | Shared kernel entry. M1 stays transport-neutral either way. |
| M1-Q2 | If the audit write fails on a *mutating* call: fail closed, or proceed and alert? | Fail closed (the brief's recommendation). Reads degrade and alert. |
| M1-Q3 | Mirror `gen_ai.*` names pinned to a `semantic-conventions-genai` commit SHA, since there are no releases? Also mirror the MCP conventions once verified? | Yes to the SHA pin. MCP mirror only after verifying the attribute names. |
| M1-Q4 | Reject an invalid caller-supplied correlation ID (`INVALID_INPUT`), or replace it with a generated one and warn? | Reject: silent replacement breaks the caller's join key. |
| M1-Q5 | Approve the M1 dependencies above? | Yes |
| M1-Q6 | Audit chain granularity: per principal? | Yes |

### Delivery (one concern per PR)

1. Settings, `ids`, ContextVar, `ObservedEntry` skeleton.
2. Events, sampling, redaction (pure) and property tests.
3. Telemetry port, OTel adapter, in-memory fakes.
4. Migration runner, audit chain, Postgres audit store, integration tests.
5. Event store, TraceQuery, `trace` CLI.
6. Sentinel plugin and the §8.10 acceptance suite.

---

## 2026-09-22 — Session 2: M0 approved and implemented

### Approvals (owner: all recommendations, gitleaks, GitHub Actions)

| ID | Decision | Status |
| --- | --- | --- |
| D-005 | Q-001–Q-006 answered as recommended: M0 dependency list, gitleaks, workspace layout, kernel draft goes to Romik, derived idempotency keys dropped, GitHub Actions, `main` with `feat/`/`fix/`/`docs/` branches. | approved by owner |
| D-006 | Recommendations for R-001, R-004, R-005, R-007, R-009, R-011–R-018 adopted. | approved by owner |
| D-007 | Recommendations for R-002, R-003, R-006, R-008, R-010 adopted on our side. They change shared interfaces, so they stay draft until Romik agrees. | approved by owner, pending-romik |

### What was built

- **uv workspace:** root project `adapter-verify` plus member `packages/adapter-kernel`. Python pinned to 3.12 (`.python-version`). `uv.lock` resolves 52 packages, with hashes.
- **Kernel draft** (`adapter_kernel`): `errors`, `context`, `pipeline`, `meta`, `tooldef`, `classification`, `shape`, `jsontypes`. Every model is frozen, strict and closed through a shared `KernelModel` base. R-002/R-003/R-008/R-009 are applied: `DeliveryStatus`, `RetryPolicy` with a `retry_after_ms` validator, `RequestContext`/`CallContext`, and a shape hash over `source_id` + operations only.
- **Tests:** 49 unit and property tests, 100% line and branch coverage. The property tests show that serialization and the hash are independent of order, that a JSON round trip is lossless, and that equal structure ⇔ equal hash.
- **`scripts/gen_spec.py`:** generates the SPEC enum/model/error tables and `docs/schemas/*.json`. `--check` is the CI freshness gate.
- **CI** (`.github/workflows/ci.yml`): lint, types, architecture, test, lockfile, spec-fresh, secrets, deps-audit, and sbom (on tags). Actions are pinned by commit SHA.
- **Repo hygiene:** `.gitignore` (env files excluded), `.gitattributes` (LF everywhere), `CODEOWNERS`.
- Verified locally: ruff, mypy (18 files), import-linter (2 contracts), pytest with the `ci` Hypothesis profile, `uv lock --check`, spec freshness, and pip-audit (no known vulnerabilities).

### Decisions made while implementing (owner to confirm)

| ID | Decision | Rejected alternatives | Status |
| --- | --- | --- | --- |
| D-008 | Build backend is **`uv_build`**, Astral's backend that ships with uv. Every package needs *some* build backend, and this one is part of the toolchain we already approved. | `hatchling`: a third-party backend with a separate release cadence. `setuptools`: heavier config, and an editable-install quirk with `src/` layouts. | pending |
| D-009 | gitleaks runs as the **release binary, verified by SHA-256**, not through `gitleaks/gitleaks-action`. | The action needs a paid license key for organization-owned repos. If the repo ever moves to an org, CI would break silently. | pending |
| D-010 | `pip-audit` fails on **any** known vulnerability, not only high/critical as M0 §3 said. pip-audit can't filter by severity. A failing advisory can be waived with an explicit `--ignore-vuln ID` in the workflow, which leaves a reviewed trail. | Filtering on severity would need a second tool (e.g. OSV-Scanner). | pending |
| D-011 | `ToolDefinitionView` is **left out** of the kernel draft. Its `status` values belong to Romik's lifecycle, and inventing them would break brief §0.5. Only `Behavior` and `Sensitivity` are defined. | Stub the model with `status: str`: that's a typed hole that would leak into §5 and §9 code. | pending-romik |
| D-012 | The JSON aliases module is `jsontypes.py`, not `json.py` as M0 §1 said. A module named `json` inside the package is confusing next to the stdlib module. | — | approved (cosmetic) |
| D-013 | `pydantic-settings` isn't added yet. There's no configuration until M1, and an unused dependency is just noise. | — | approved (deferral) |
| D-014 | Two Hypothesis profiles: `dev` (50 examples) and `ci` (500, derandomized), selected with `HYPOTHESIS_PROFILE`. | — | pending |

### What broke

- **Correction:** the first `ruff format .` also reformatted the Python snippet in Session 1's kernel draft. Semicolon-joined enum members were split onto separate lines. The content didn't change. Markdown is now excluded from ruff formatting so the log stays append-only.
- The Docker daemon wasn't running locally, so gitleaks wasn't run locally. It runs in CI. Integration tests (M1+) need Docker Desktop started.

### Deferred

- The first push is waiting on the `shaswat28` repo URL (D-001). CI hasn't run on GitHub yet.
- Romik's handle in `CODEOWNERS` for the kernel.

---

## 2026-09-22 — Session 1: brief review and M0 proposal

### What happened

- Read the brief (`execution-verification-brief.md`, §0–16).
- Ran `git init -b main` locally. There is no remote.
- Installed `uv` 0.12.17 with `pip install --user`. It isn't on PATH, so run it as `python -m uv`.
- Checked the environment: Windows 11, Python 3.12.10, Docker 29.4.1 (so testcontainers can run here).
- No application code was written, as §16 requires.

### Decisions

| ID | Decision | Status |
| --- | --- | --- |
| D-001 | Git remote: local only. The owner will get access to a private repo on the `shaswat28` GitHub account and provide the URL. Nothing is pushed until then. | approved by owner |
| D-002 | Python 3.12. UUIDv7 (RFC 9562) is implemented in-house (~20 lines plus property tests), with no new dependency. Swap to stdlib `uuid.uuid7()` when we reach 3.14. | approved by owner |
| D-003 | `uv` installed with `pip install --user`. | approved by owner |
| D-004 | Monorepo vs separate repos is still open. The layout must work either way (see M0 §1). | approved by owner (design for both) |

---

### Brief review: ambiguities, contradictions, risks (most important first)

Each item gives a recommendation. None of these is decided.

**R-001 · Contradiction: is a key required on mutating calls, or derived when missing?**
§7.1 says a mutating tool without a key is rejected with `IDEMPOTENCY_KEY_REQUIRED`. §7.2 says that when the agent supplies no key, the adapter derives one as `{correlation_id}:{tool}:{step}`. Both can't be true.
*Recommend:* always reject when there's no key, and drop the derived form from Phase 1. A derived key is only safe if the step number is stable, and we can't verify that. The failure mode is a duplicate customer action. If orchestrators want derivation, the orchestrator derives the key and sends it, so it arrives as a supplied key. `pending`

**R-002 · §7 can't decide state from a `ToolResult` alone.**
The idempotency stage sits in the middle of the chain (§3.5). All it gets back from `call_next` is a final `ToolResult`, after drift absorption, translation and output validation. From that it can't tell apart the three cases §7.4 depends on: *never sent*, *sent with no answer*, and *acknowledged*. Example: `UPSTREAM_UNAVAILABLE` could be a connection refused before sending (safe to retry) or a timeout after sending (must become `UNKNOWN`).
*Recommend:* connectors report a kernel-level `DeliveryStatus` (`NOT_SENT | SENT_NO_RESPONSE | ACKED | REJECTED_NO_EFFECT`). It travels on an internal part of the result that §7 reads and is stripped before the response reaches the agent. This changes a shared interface (§3.5), so it needs Romik. `pending-romik`

**R-003 · Retry safety depends on delivery, not only on the error code.**
`retry_safe: bool` can't express `DUPLICATE_IN_PROGRESS`'s "yes, later". It is also wrong for `UPSTREAM_UNAVAILABLE` on a mutating call when the request was actually sent.
*Recommend:* replace the bool with `RetryPolicy = NEVER | IMMEDIATE | AFTER_DELAY`, and have the result carry `retry_after_ms`. When §7 sees `SENT_NO_RESPONSE`, it converts the error to `RECONCILIATION_PENDING`, so an agent is never told to retry an action whose outcome is unknown. `pending-romik`

**R-004 · Upstream succeeds, then translation or output validation fails.**
The backend acknowledged the call, so the customer's account changed. Then §2/§3 raises `CONTRACT_VIOLATION` or `DRIFT_BLOCKED`. The brief doesn't say what §7 stores. If the row becomes `FAILED_RETRYABLE`, the retry duplicates the action.
*Recommend:* any `ACKED` delivery ends as `COMPLETED`, with an `error_code` column recording the post-upstream failure. A replay returns the same error and nothing re-executes. The failure also alerts the owner, because an effect happened that the agent can't see. `pending`

**R-005 · A late completion from a "crashed" attempt.**
If a call runs longer than its lease, the sweeper marks the row `UNKNOWN`. The original attempt may then return with a definitive acknowledgement. The brief only allows humans to resolve `UNKNOWN`.
*Recommend:* store an `attempt_id` on the row as a fencing token. Only the attempt holding the matching `attempt_id` may move `UNKNOWN → COMPLETED`, and only on `ACKED`. That transition is audited. Everything else still goes to a human. Also required: a sweeper job that moves expired `RESERVED` rows to `UNKNOWN`. The brief implies it but doesn't list it. `pending`

**R-006 · Stage order: access control needs a version before the lifecycle stage resolves one.**
§9 checks `semantic_version` against a granted range, but it runs before the §4 lifecycle gate. If an agent calls `order.get` without a version, something must resolve the version first. The brief doesn't say what, or whether the version travels in the tool name (`order.get@1`) or as a separate field.
*Recommend:* a pure `resolve_version(tool, requested)` from Romik's registry runs in the pipeline entry, before §9. `pending-romik`

**R-007 · MCP transport decides how identity works.**
§9's token checks (signature, audience, expiry) assume bearer tokens. That means MCP Streamable HTTP with its authorization flow. stdio has no token. The `tools/list` filter also needs identity at list time.
*Recommend:* Phase 1 serves over Streamable HTTP only, and any stdio use is dev-only with a fake verifier that is refused in production config. Which transport is used affects §9's design. `pending`

**R-008 · The `CallContext` is frozen, but `agent_id` only exists after authentication.**
§8 runs before §9, so the correlation ID exists before `agent_id` does. A frozen model with a required `agent_id` can't represent the pre-authentication phase.
*Recommend:* two types. `RequestContext` is pre-authentication and has no `agent_id`. `CallContext` is post-authentication and has everything. Authentication is the only way to get from one to the other. Rejected: a nullable `agent_id`, because every stage would need a None check and one could forget it. Rejected: a mutable context, because it breaks `frozen` and makes stage behavior depend on order in hidden ways. `pending-romik`

**R-009 · The §5.5 rule set doesn't cover everything in `ShapeModel`, which breaks the stated property.**
Property: "`diff(a,b)` empty ⇔ hashes equal". The shape has operations, `errors`, `format` and nullability on inputs, but no rules cover operation added/removed, error set changes, `format` changes, `INPUT_BECAME_OPTIONAL`, `OUTPUT_BECAME_REQUIRED`, `OUTPUT_ENUM_VALUE_REMOVED`, `INPUT_ENUM_VALUE_ADDED`, or input nullability. Two shapes that differ only in these would hash differently and still produce an empty diff. The hash's inputs aren't fully specified either: does it include `source_version` or `extractor_version`?
*Recommend:* the hash covers `source_id + operations` only. Add the missing rules, drafted in the M4 design. Add a catch-all `UNCLASSIFIED_CHANGE → UNKNOWN` so that any structural difference produces a finding. That makes the property hold by construction, and it fails closed. `pending`

**R-010 · Some rules only make sense in one of the checks.**
Upstream sources have no semver, so "COMPATIBLE with MINOR bump" only applies to the canonical-contract check. Direction also flips for upstream sources: the adapter is the consumer, so a removed upstream output field only breaks us if a mapping reads it.
*Recommend:* each rule declares `applies_to: [upstream, canonical]`. The upstream diff uses Romik's mappings, so a field no mapping uses produces an informational finding instead of `BREAKING`. That cuts false blocks sharply. `pending-romik`

**R-011 · The audit hash chain fights horizontal scaling (§11).**
A single global chain means every audited write across every instance must be serialized. And if audit failure is fail-closed on mutating calls (open question §15), the audit DB is on the critical path with a global lock.
*Recommend:* one chain per partition (e.g. per `agent_id`, or hash-bucketed). Periodically anchor each chain's head into a global chain. That still detects tampering but removes the global lock. `pending`

**R-012 · Golden-task sandbox vs the LLM provider.**
"No egress" and "no real credentials" (§6.2) conflict with the agent under test and the judge, which both call an LLM API with a real API key. The brief also doesn't say who provides the agent under test (prompt, orchestrator, tool-selection logic). Those likely belong to other teams.
*Recommend:* the egress allow-list is stubs plus one pinned LLM endpoint. The LLM key is injected into the harness process only and is never reachable from the adapter's secret-manager port. Sentinel scanning covers the LLM request too. The owner needs to confirm where the agents come from. `pending`

**R-013 · The quarantine rule could quietly weaken the gate.**
A quarantined write task (which needs 3/3) that no longer gates means promotions go through untested.
*Recommend:* while a task is quarantined, promotion of every tool that task touches stays blocked until the task is fixed or the owner records an explicit, audited waiver. `pending`

**R-014 · Recording CI overrides in the audit store gives CI access to production.**
§5.7 says overrides are "always logged (§8)". Doing that literally means CI holds credentials for the production audit store.
*Recommend:* CI records overrides in git (the PR label event plus a committed report artifact). A separate job ingests them into the audit store later. CI never holds production credentials. `pending`

**R-015 · The 15 ms p99 overhead budget vs synchronous durability.**
A mutating call makes about four database round trips (reserve, audit, complete, audit) plus redaction. That's feasible within one region and tight otherwise.
*Recommend:* measure first. Report reads and mutating calls as separate budgets. Don't change the design to hit a target nobody has confirmed. `pending`

**R-016 · Redis has no job in §5–9.**
It's listed in §2 and §11, but none of the §5–9 components need it. Policies live in memory and idempotency lives in Postgres.
*Recommend:* no Redis in Phase 1 until a component needs it. `pending`

**R-017 · Retention across the idempotency table and the payload store.**
Replay reads `result_ref` from the payload store. If payloads expire before idempotency rows, replay breaks, and `UNKNOWN` rows are never deleted. The payload store's backend isn't named anywhere.
*Recommend:* payload retention must be at least the idempotency retention, and payloads referenced by `UNKNOWN` rows are pinned until the row resolves. `pending`

**R-018 · Smaller clarifications.**
- `ContextVar` and the registries vs "no module-level singletons". *Recommend:* allow a module-level `ContextVar` as an explicit exception, since it's context-local and not shared state. Build registries in the composition root.
- "Domain packages" for the coverage floor. *Recommend:* every `*/domain/` plus every `service.py`.
- oasdiff is a Go binary, not a Python package. *Recommend:* run it as a container image pinned by digest.
- `x-sensitivity` covers canonical fields only. Raw upstream bodies have no annotations, so they are masked entirely. That's correct under fail-closed, but it makes debugging drift harder.

`pending`

---

### M0 design proposal

#### 1. Repository layout — works with either repo shape (D-004)

A **uv workspace**. The kernel is its own workspace member from day one, so it can later move into Romik's monorepo or ship as a separately versioned package without import changes.

```
techM/
├── pyproject.toml              # root project = adapter-verify; [tool.uv.workspace]
├── uv.lock                     # committed, hashes included (uv default)
├── packages/
│   └── adapter-kernel/         # shared kernel (§3), separately versioned
│       ├── pyproject.toml
│       └── src/adapter_kernel/
│           ├── errors.py       # ErrorCode, RetryPolicy, ErrorSpec registry
│           ├── context.py      # RequestContext, CallContext
│           ├── pipeline.py     # ToolRequest, ToolResult, Stage, Next, DeliveryStatus
│           ├── meta.py         # ResponseMeta
│           ├── tooldef.py      # Behavior, Sensitivity, ToolDefinitionView
│           ├── classification.py
│           ├── shape.py        # ShapeModel (§5.2)
│           ├── json.py         # JsonValue / JsonObject aliases (no Any)
│           └── py.typed
├── src/adapter_verify/         # layout exactly as brief §4
├── tests/                      # layout exactly as brief §4
├── scripts/gen_spec.py         # regenerates the generated sections of SPEC.md
├── baselines/  policies/  golden_tasks/  migrations/
├── docs/SPEC.md
├── .github/workflows/ci.yml    # GitHub Actions (assumed; see Q-005)
├── .github/CODEOWNERS          # baselines/, policies/, migrations/, packages/adapter-kernel/
└── DESIGN.md  CONTRIBUTING.md  README.md
```

- **If monorepo:** Romik's package becomes another workspace member. The kernel stays where it is.
- **If separate repos:** `packages/adapter-kernel` moves to its own repo, and both halves depend on a pinned version of it.

Neither path touches code in `adapter_verify`.

**Enforcing the hexagonal boundary:** use `import-linter` contracts in CI:
- `*/domain` may not import `*/adapters`, `asyncpg`, `httpx`, `opentelemetry`, `os`, or `socket`.
- Only `composition.py` may import `adapters`.
- `adapter_kernel` may not import `adapter_verify`.

Reviewers enforcing this by eye will miss things.

#### 2. Tooling configuration

| Tool | Configuration |
| --- | --- |
| ruff (lint) | Explicit rule list, not `ALL`: `E W F I B UP SIM RUF S ASYNC PT TRY BLE T20 G LOG DTZ N ANN ARG RET PIE PERF C4 FBT ERA TID PL`. These cover things the brief asks for: `T20` bans print, `G` bans f-string log messages, `BLE` bans blind except, `DTZ` bans naive datetimes, `ASYNC` catches blocking calls in async code, and `S` is the bandit security checks. |
| ruff (format) | Line length 100. CI runs `--check`. |
| mypy | `strict = true` with the pydantic mypy plugin (`init_forbid_extra`, `init_typed`, `warn_required_dynamic_aliases`). `disallow_any_explicit` is on for `adapter_kernel` and every `*/domain` package. |
| pytest | `asyncio_mode = "strict"`, `--strict-markers`, `filterwarnings = ["error"]`, markers `unit property integration concurrency golden_selftest`. The default run excludes `integration` and `concurrency`, and CI runs those as separate jobs. |
| coverage | `branch = true`. `fail_under = 90` applied to the domain packages only (see R-018). |
| hypothesis | A `ci` profile (more examples, `derandomize=True`) and a `dev` profile (fast). |
| settings | `pydantic-settings` reading `ADAPTER_*` environment variables, validated at startup. |

#### 3. CI jobs (GitHub Actions, assumed)

Actions are pinned by commit SHA. Top-level `permissions: contents: read`. No secrets in any M0 job.

| Job | Runs | Blocks merge |
| --- | --- | --- |
| `lint` | `ruff check`, `ruff format --check` | yes |
| `types` | `mypy` | yes |
| `architecture` | `lint-imports` | yes |
| `test` | unit + property tests, coverage gate | yes |
| `lockfile` | `uv lock --check` (lockfile matches pyproject) | yes |
| `spec-fresh` | `scripts/gen_spec.py`, then `git diff --exit-code docs/SPEC.md` | yes |
| `secrets` | secret scanner over the diff, plus a check that no `.env*` file (other than `.env.example`) is tracked | yes |
| `deps-audit` | `pip-audit` against `uv export --hashes` | yes (high/critical) |
| `sbom` | CycloneDX SBOM, only on release tags | n/a |
| `integration` / `concurrency` | added in M1/M3, when their first test exists | yes |

Rejected: adding placeholder jobs for integration now. A green job that runs nothing gives false confidence.

#### 4. Documentation and SPEC generation

- `docs/SPEC.md` has hand-written *why* sections and generated *what* sections. Generated sections sit between `<!-- BEGIN GENERATED: <name> -->` / `<!-- END GENERATED -->` markers.
- `scripts/gen_spec.py` produces:
  - the error table from the `ErrorCode` registry
  - JSON Schemas from the Pydantic models (`model_json_schema`)
  - the rule table from the rule YAML (M4)
  - the CLI reference from the CLI definitions
  - the config table from the settings model
- The `spec-fresh` CI job fails on any difference.

#### 5. Shared kernel draft — to send to Romik (`pending-romik`)

Changes from brief §3 are marked **Δ**, with the matching R-item.

```python
# errors.py
class RetryPolicy(StrEnum):  # Δ R-003: replaces retry_safe: bool
    NEVER = "never"
    IMMEDIATE = "immediate"
    AFTER_DELAY = "after_delay"


class ErrorCode(StrEnum):
    INVALID_INPUT = "INVALID_INPUT"
    NOT_AUTHORIZED = "NOT_AUTHORIZED"
    VERSION_NOT_PERMITTED = "VERSION_NOT_PERMITTED"
    SCOPE_VIOLATION = "SCOPE_VIOLATION"
    IDEMPOTENCY_KEY_REQUIRED = "IDEMPOTENCY_KEY_REQUIRED"
    IDEMPOTENCY_KEY_CONFLICT = "IDEMPOTENCY_KEY_CONFLICT"
    DUPLICATE_IN_PROGRESS = "DUPLICATE_IN_PROGRESS"
    RECONCILIATION_PENDING = "RECONCILIATION_PENDING"
    DRIFT_BLOCKED = "DRIFT_BLOCKED"
    UPSTREAM_UNAVAILABLE = "UPSTREAM_UNAVAILABLE"
    CONTRACT_VIOLATION = "CONTRACT_VIOLATION"
    INTERNAL = "INTERNAL"


class ErrorSpec(BaseModel, frozen=True, strict=True, extra="forbid"):
    retry: RetryPolicy
    agent_message: str  # fixed text; never interpolated


ERROR_SPECS: Mapping[ErrorCode, ErrorSpec]  # immutable; a test checks it is total over ErrorCode


class AdapterError(BaseModel, frozen=True, strict=True, extra="forbid"):
    code: ErrorCode
    retry_after_ms: int | None = None  # Δ R-003; set only when retry is AFTER_DELAY


# context.py — Δ R-008: separate types before and after authentication
class RequestContext(BaseModel, frozen=True, strict=True, extra="forbid"):
    correlation_id: str
    correlation_id_generated: bool  # Δ lets §8 echo it back only when we generated it
    trace_id: str
    tool: str
    requested_version: str | None  # Δ R-006
    idempotency_key: str | None


class CallContext(BaseModel, frozen=True, strict=True, extra="forbid"):
    request: RequestContext
    agent_id: str
    semantic_version: str  # resolved (R-006)
    behavior: Behavior


# pipeline.py
class DeliveryStatus(StrEnum):  # Δ R-002: reported by connectors
    NOT_SENT = "not_sent"
    SENT_NO_RESPONSE = "sent_no_response"
    ACKED = "acked"
    REJECTED_NO_EFFECT = "rejected_no_effect"


class ToolRequest(BaseModel, frozen=True, strict=True, extra="forbid"):
    arguments: JsonObject


class ToolSuccess(BaseModel, frozen=True, strict=True, extra="forbid"):
    kind: Literal["success"] = "success"
    content: JsonObject
    meta: ResponseMeta


class ToolFailure(BaseModel, frozen=True, strict=True, extra="forbid"):
    kind: Literal["failure"] = "failure"
    error: AdapterError
    meta: ResponseMeta


class ToolResult(BaseModel, frozen=True, strict=True, extra="forbid"):
    outcome: Annotated[ToolSuccess | ToolFailure, Field(discriminator="kind")]
    delivery: DeliveryStatus | None  # Δ internal only; removed at the MCP boundary


class Next(Protocol):
    async def __call__(self, ctx: CallContext, request: ToolRequest) -> ToolResult: ...


class Stage(Protocol):
    name: str

    async def __call__(
        self, ctx: CallContext, request: ToolRequest, call_next: Next
    ) -> ToolResult: ...


# meta.py — brief §3.4 unchanged
class ResponseMeta(BaseModel, frozen=True, strict=True, extra="forbid"):
    correlation_id: str
    absorbed: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    replayed: bool = False
    adapter_version: str | None = None


# tooldef.py — the fields §5–9 read (brief §3.6); Romik's format adds these annotations
class Behavior(StrEnum):
    READ_ONLY = "read_only"
    MUTATING = "mutating"
    DESTRUCTIVE = "destructive"


class Sensitivity(StrEnum):  # schema keyword: x-sensitivity; missing means secret (fail closed)
    PUBLIC = "public"
    INTERNAL = "internal"
    PII = "pii"
    SECRET = "secret"


# classification.py (§3.1) and shape.py (§5.2): as in the brief, plus the hash definition in R-009.
```

**Where the correlation ID and idempotency key travel (brief §3.3):**

| Option | Assessment |
| --- | --- |
| **A. MCP request `params._meta`, namespaced keys** (e.g. `<org-domain>/correlation-id`, `<org-domain>/idempotency-key`) | **Recommended.** The orchestrator sets these, not the model. The tool `inputSchema` stays pure, so the fingerprint (§7.3) covers only business arguments. |
| B. Tool arguments | Rejected. The key would be generated by the LLM, and an LLM retry often produces a *new* key, which defeats §7 entirely. It also clutters every `inputSchema`. |
| C. HTTP headers | Rejected. Tied to one transport, and some MCP clients can't set them per call. |

#### 6. Dependencies requested for M0 (none installed yet)

| Package | Purpose | Kind |
| --- | --- | --- |
| `pydantic` v2, `pydantic-settings` | models, config | runtime |
| `ruff`, `mypy` | lint/format, types | dev |
| `pytest`, `pytest-asyncio`, `pytest-cov`, `hypothesis` | tests | dev |
| `import-linter` | architecture boundaries | dev |
| `pip-audit` | vulnerability scan | dev/CI |
| `cyclonedx-bom` | SBOM | CI (release) |
| `gitleaks` (Go binary, pinned action) **or** `detect-secrets` (Python) | secret scan | CI. Recommend gitleaks: better default rules, and it runs in CI only, not as a project dependency. |

Deferred to later milestones: `click` (CLI, M1), `opentelemetry-*` (M1), `asyncpg` + `testcontainers` (M1/M3), an RFC 8785 library (M3), `mcp` SDK (whenever the pipeline wiring lands).

#### 7. Options considered and rejected (M0)

| Area | Rejected | Why |
| --- | --- | --- |
| Package manager | Poetry, pip-tools | Brief mandates uv. uv's lockfile has hashes by default. |
| Lint/format | black + isort + flake8 | ruff covers all three and is faster. The brief mandates ruff. |
| Kernel placement | subpackage `adapter_verify.kernel` | Romik would have to depend on all of adapter_verify to use it. |
| Kernel placement | git submodule | Painful workflow, especially on Windows. The workspace member gives the same separation. |
| Kernel placement | publish to a private index now | Premature before Romik agrees on contents. |
| Layout | flat (no `src/`) | The `src/` layout stops tests from accidentally importing the working tree instead of the installed package. |
| Task runner | Makefile / nox / tox / just | `make` is awkward on Windows. `uv run <cmd>` covers everything, and the exact commands go in CONTRIBUTING.md. |
| Type checker | pyright instead of mypy | Brief mandates mypy. Can be added later as a second check if wanted. |
| ruff `select = ["ALL"]` | — | New ruff releases would add rules automatically, so CI would change without a code change. An explicit list is reproducible. |

---

### Proposed additions beyond the brief (creative, each small and optional)

| # | Idea | Value | Cost |
| --- | --- | --- | --- |
| X-1 | A **`Clock` port** everywhere time matters (leases, expiry, token checks) | Deterministic lease and expiry tests with no sleeps. Crash tests become fast. | One small protocol |
| X-2 | **Fencing `attempt_id`** on idempotency rows (R-005) | Closes the late-completion race safely | One column |
| X-3 | **Policy snapshot hash** on every access decision event | Any past decision can be replayed exactly with `access explain --at <hash>` | Hash computed at policy load |
| X-4 | **Shadow policy mode**: evaluate a proposed policy beside the live one and log differences without enforcing | Policy changes can be tested against real traffic before they take effect | One evaluator call. Needs owner approval because it widens what gets logged. |
| X-5 | **pytest sentinel plugin**: fixtures inject sentinel secrets and PII; an autouse fixture scans every captured sink (logs, spans, payload store, error bodies) after every test | §8.10/§9.10 become a property of the whole suite, not a few dedicated tests | One conftest plugin |
| X-6 | **Fault-injecting stub connector** (fail before send, time out after send, ack then drop, slow ack) | Every branch of §7.4 is covered on purpose. The golden sandbox reuses it. | One stub |
| X-7 | **Trace-to-golden scaffolder**: `adapter-verify golden scaffold --correlation-id X` drafts a task file from a real trace, with payloads replaced by synthetic placeholders for a human to fill | Turns production incidents into regression tests | M5 add-on |
| X-8 | **SARIF output** from contract CI | Findings show inline on the PR diff, not only in a comment | One formatter |
| X-9 | **Mapping-aware upstream diff** (R-010) | Big cut in false `BREAKING` blocks from unused upstream fields | Needs Romik's mapping registry API |
| X-10 | **Ship in-memory fakes of every port** inside the package, not in tests | The same fakes serve unit tests, golden sandbox, and local dev. They have to satisfy the same Protocols, so mypy catches drift. | Discipline, not code |

---

### Open questions blocking M0 implementation

| ID | Question | Recommendation |
| --- | --- | --- |
| Q-001 | Approve the M0 dependency list (§6)? Which secret scanner? | Approve the list, with gitleaks |
| Q-002 | Approve the workspace layout with the kernel as its own member (§1)? | Yes |
| Q-003 | Approve the kernel draft (§5) to send to Romik, including the Δ changes? | Yes, as a proposal |
| Q-004 | R-001: drop derived idempotency keys from Phase 1? | Yes |
| Q-005 | Which CI platform: GitHub Actions, or does the company mandate another? | GitHub Actions, since the remote will be GitHub |
| Q-006 | Default branch name `main` and one-concern PRs into it. Any branch protection or naming conventions? | `main` + `feat/…`, `fix/…`, `docs/…` |

R-002 through R-017 don't block M0. They must be settled before the milestone they affect: R-002–R-005 → M3; R-006–R-008 → M1/M2; R-009/R-010/R-014 → M4; R-012/R-013 → M5; R-011 → M1.

### Deferred

- All application code (as §16 requires).
- Tooling files (`pyproject.toml`, CI workflow, `.gitignore`, CODEOWNERS): written once M0 is approved.
- The kernel proposal message to Romik: drafted after Q-003 is answered.
