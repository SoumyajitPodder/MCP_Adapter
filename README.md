# adapter-verify

The execution and verification layer of an MCP adapter that puts stable, versioned tools in front of legacy telecom backends. It answers one question: **can we safely and reliably execute and verify this tool call?**

> **Status:**
> - Done: M0 (foundation), M1 (logging and audit), M2 (access control), M3 (duplicate prevention), M4 (contract CI), M5 (golden tasks, with a reference agent and judge on the NVIDIA API catalog or Gemini).
> - Next: the first full live golden run and canaries (NVIDIA key); then the CI golden job, once a network-isolated runner exists.

## What it does

| Component | When it runs | Purpose | State |
| --- | --- | --- | --- |
| Contract testing | CI | Blocks upstream or contract changes that would break agents | **M4, done** |
| Golden-task regression | CI / sandbox | Proves agent behavior still holds after tool, description, mapping or model changes | **M5 done** (live runs need a model API key) |
| Duplicate prevention | every mutating call | Retries of a mutating call take effect exactly once; if the outcome is unknown, it goes to a human instead of guessing | **M3, done** |
| Correlation-ID logging | every call | One ID reconstructs a whole decision chain, with fail-closed redaction and a tamper-evident audit trail | **M1, done** |
| Access control | every call | Deny by default, enforced at both tool discovery and tool execution | **M2, done** |

## Setup

You need:
- Python 3.12 and [uv](https://docs.astral.sh/uv/) 0.12 or newer;
- Docker, for integration tests and for a local Postgres.

```bash
uv sync
```

## How to run

To run the checks CI runs (`CONTRIBUTING.md` lists every command):

```bash
uv run pytest --cov
```

Integration tests start a throwaway Postgres 17 container:

```bash
uv run pytest -m integration
```

The operator CLI needs a database. Set `ADAPTER_DATABASE_DSN` (a `postgresql://` URL, treated as a secret) in your environment. Never commit it; CI fails if an `.env` file is tracked. Then:

```bash
uv run adapter-verify db migrate
```

To reconstruct one job's steps, oldest first:

```bash
uv run adapter-verify trace --correlation-id job-42
```

Two audit commands should run on a schedule (not automated yet):

```bash
uv run adapter-verify audit anchor
```

```bash
uv run adapter-verify audit verify
```

- `audit anchor` bounds how much recent audit history could be truncated undetected. Run it every minute.
- `audit verify` detects tampering and exits 1 if it finds any. Run it at least daily.

Duplicate prevention needs two scheduled jobs as well: `idem sweep` (every 15 s or so) turns expired reservations into `UNKNOWN`, and `idem purge` (daily) applies retention. A person settles `UNKNOWN` rows:

```bash
uv run adapter-verify idem unknown
uv run adapter-verify idem resolve --agent <a> --tool <t> --key <k> --as failed --reason "no order upstream" --operator <id>
```

Every option and exit code is listed in `docs/SPEC.md` §11. Configuration keys are in §4.

Access policies live in `policies/` (one file per agent), checked by `uv run adapter-verify policy lint`. To see why an agent is allowed or denied a tool:

```bash
uv run adapter-verify access explain --agent order-status-agent --tool order.get
```

Golden tasks live in `golden_tasks/`; `uv run adapter-verify golden lint` checks them in CI. Live runs call a model API (NVIDIA API catalog by default, or Gemini): copy `.env.example` to `.env` (git-ignored) and paste your key there. Free tiers may use submitted content; golden data is synthetic only.

```bash
uv run adapter-verify golden calibrate
```

```bash
uv run adapter-verify golden run --all
```

```bash
uv run adapter-verify golden canaries
```

Contract checks run in CI; locally:

```bash
uv run adapter-verify contract check
```

After an intended upstream change, `baseline accept --source <id>` writes a new baseline for review. After a contract change is approved, `contract release --tool <t> --version <v>` locks it.

There is no long-running server yet. Who owns the MCP server process is an open question (`DESIGN.md` M1-Q1). The adapter's entry point, `ObservedEntry`, is transport-neutral so it can plug into whichever server is chosen.

## How the pieces fit

```
MCP server (owner TBD) ──InboundCall──► ObservedEntry  (§8: correlation ID, tool.call span, event)
                                            │
                                            ▼
                         authentication → stages (§9 access, §4 lifecycle, validate,
                                                  §7 idempotency, connector, §3 drift, §2 translate)
                                            │
          spans ─► OTLP exporter   events ─► buffered sink ─► call_events (Postgres)
          bodies ─► payload store (refs only elsewhere)
          security decisions ─► audit_log hash chains (Postgres) ◄─ audit anchor / verify
```

Access (§9) and idempotency (§7) stages exist; the others arrive with Romik's §1–4.

## Dependencies

- **Runtime:** `pydantic` v2, `pydantic-settings`, `opentelemetry-api`/`-sdk`/`-exporter-otlp-proto-http`, `asyncpg`, `click`, `pyyaml`, `pyjwt[crypto]`, `rfc8785`, `google-genai`.
- **Dev and CI:** `ruff`, `mypy`, `asyncpg-stubs`, `pytest`, `pytest-asyncio`, `pytest-cov`, `hypothesis`, `import-linter`, `pip-audit`, `testcontainers`. CI also uses the `gitleaks` binary and `cyclonedx-bom` (release builds only).
- Exact versions and hashes are in `uv.lock`.

## Costs

- Per suite run: one agent conversation per task run (5 tasks × 3 runs, a few requests each) plus one judge call per judged run and 7 calibration calls. `golden canaries` adds a baseline suite plus one partial suite per canary. Token counts are recorded in each results file and will be written here after the first real run.
  - NVIDIA API catalog (default): the free key comes with trial credits (about one per request) and a per-minute rate limit; 429s are retried with backoff.
  - Gemini free tier: 20 requests per model per day (measured 2026-09-26), so a full cycle spans several days.
- The audit trail writes one row per audited decision. Anchoring adds one row per changed chain per run.

## Constraints

- Phase 1 exclusions: brief §1.4.
- All fixtures are synthetic. No real customer, account or network data.
- The production payload store, identity provider, secret manager and observability backend are undecided (brief §15). Their ports exist, and in-memory fakes stand in for them.

## Documentation

- `docs/SPEC.md`: complete reference. The tables are generated from code and checked in CI.
- `DESIGN.md`: decision log, including open questions and designs awaiting approval.
- `docs/proposals/kernel-v0-for-romik.md`: the shared-kernel proposal.
- `CONTRIBUTING.md`: commands, layout, rules and gotchas.
