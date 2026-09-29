# Contributing

Scope and rules come from the engineering brief (LLD §5–9). `DESIGN.md` records every decision and its approval status; `docs/SPEC.md` is the reference.

## Rules

- **Design first:** each milestone's design goes in `DESIGN.md` and needs owner approval before it's implemented.
- **Ask before:** adding a dependency, changing a shared-kernel interface, or deviating from the brief.
- **Synthetic data only:** no real AT&T endpoints, payloads or credentials. Mark unknowns `TODO(owner):`.
- **Pushes:** only to `SoumyajitPodder/MCP_Adapter`, branch `shaswat_changes`.
- **Commits:** one concern per commit, each with tests, types and docs updated.

## Commands

From `contract_testing/` (the uv workspace and lock are at the repository root):

```bash
uv sync
uv run ruff check . && uv run ruff format --check .
uv run mypy
uv run lint-imports                       # architecture contracts
uv run pytest --cov                       # unit + property (coverage floor 90%)
uv run pytest -m integration              # real Postgres via Docker
uv run adapter-verify golden lint         # golden task files
uv run adapter-verify golden run --all    # live: needs a model API key in the root .env (see .env.example)
uv run python scripts/gen_spec.py         # regenerate SPEC tables and docs/schemas
uv run python scripts/gen_spec.py --check # CI freshness gate
uv lock --check
```

## Layout

| Path | Contents |
| --- | --- |
| `../kernel/` | Shared kernel draft (pending Romik): frozen, strict Pydantic models and Protocols |
| `src/adapter_verify/common/` | Clock/entropy ports, `FrozenModel`, migration planner and runner |
| `src/adapter_verify/<component>/` | `domain/` (pure), `ports.py`, `fakes.py`, `adapters/`, services |
| `src/adapter_verify/composition.py` | The only place ports are wired to adapters |
| `migrations/` | Forward-only, checksummed SQL |
| `policies/`, `catalog/`, `credentials.yaml` | Access policies, the synthetic tool catalog, tool → secret names (CODEOWNERS) |
| `golden_tasks/`, `catalog/definitions/`, `fixtures/golden/` | Golden tasks and agent configs, agent-facing tool definitions, synthetic fixtures; `quarantine.yaml` changes only by reviewed PR |
| `sources/`, `baselines/`, `contracts/`, `fixtures/samples/` | Contract CI inputs; baselines and `contracts/released.json` change only via the CLI |
| `tests/` | `unit/`, `property/`, `integration/`; `sentinels.py` (leak scan on every test); `contracts.py` (port contract suites) |

## Gotchas

- **Exclude Markdown from `ruff format`.** It rewrites code blocks inside `.md` files.
- **Keep generated files LF.** `spec-fresh` compares bytes.
- **Never edit inside `BEGIN/END GENERATED` markers in SPEC.md.** `gen_spec.py` overwrites them.
- **OTel must not record exceptions.** Messages can contain customer data. `OtelTelemetry` disables it, and a test guards it.
- **Log exceptions only via `safe_exception_summary`.** DB adapters raise port errors `from None`.
- **Report settings errors with `errors(include_input=False)`.** The DSN is a secret.
- **Redaction drops undeclared object keys.** If a field vanishes from a redacted payload, check the schema.
- **Don't mix sync tests into an asyncio-marked module.** The warning becomes an error.
- **A test that puts `SENTINEL-` values into a sink must be marked `sentinel_exempt` with a reason.**
- **Integration tests each get a fresh database.** `audit_log` forbids `TRUNCATE`.
- **Import testcontainers from `testcontainers.community.postgres`.** The old path warns, which becomes an error.
- **`MIGRATIONS_DIR` points at the repository.** An installed wheel needs `--migrations-dir`.
- **Integration tests need the Docker daemon running.**
- **Tests never see a real model key.** An autouse fixture clears the Gemini and NVIDIA keys and stops `.env` loading; adapters are tested against fake clients.
- **Gemini 3: keep temperature at the default and send the model's turns back unchanged.** Lower temperatures can loop, and dropped thought signatures break multi-turn tool calling.
