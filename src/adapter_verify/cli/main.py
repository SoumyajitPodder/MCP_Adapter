"""``adapter-verify`` entry points.

Exit codes: 0 success, 1 check failed or nothing found, 3 configuration or tool error
(matches the contract-CI convention in brief §5.10 where it overlaps).

Errors are reported without echoing inputs: settings errors can contain the database DSN,
and driver errors can quote data.
"""

import asyncio
import getpass
import os
import sys
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Final

import asyncpg
import click
from pydantic import ValidationError

from adapter_kernel.classification import worst
from adapter_kernel.shape import ProvenanceKind
from adapter_verify import composition
from adapter_verify.access.adapters.files import ConfigFileError
from adapter_verify.access.domain.evaluate import RULE_UNKNOWN_TOOL, evaluate
from adapter_verify.access.domain.lint import PolicyDocument, lint
from adapter_verify.access.domain.policy import PolicySet, ToolCatalog
from adapter_verify.common.domain.migrations import MigrationError
from adapter_verify.contract_ci.adapters.files import load_rulebook, read_shape
from adapter_verify.contract_ci.domain.differ import classify, diff
from adapter_verify.contract_ci.domain.extract import ExtractionError
from adapter_verify.contract_ci.domain.rules import CheckKind
from adapter_verify.contract_ci.service import ContractCi, ReleaseError
from adapter_verify.golden.adapters.files import (
    Workspace,
    load_canaries,
    read_results,
    with_inputs,
    write_results,
)
from adapter_verify.golden.adapters.git import GitError
from adapter_verify.golden.domain import lint as golden_lint_rules
from adapter_verify.golden.domain.canaries import CanaryKind, CanaryStatus, affected, mutate, status
from adapter_verify.golden.domain.report import render
from adapter_verify.golden.domain.results import SuiteResults, TaskVerdict
from adapter_verify.golden.domain.select import select
from adapter_verify.golden.domain.tasks import GoldenTask
from adapter_verify.golden.domain.verdict import gate
from adapter_verify.golden.ports import HarnessUnavailableError
from adapter_verify.golden.service import SuitePlan
from adapter_verify.idempotency.domain.keys import is_valid_key
from adapter_verify.idempotency.domain.records import IdempotencyRecord, RecordKey
from adapter_verify.idempotency.service import Operator, Resolution
from adapter_verify.observability.domain.ids import is_valid_correlation_id
from adapter_verify.observability.domain.trace import render_json, render_table
from adapter_verify.observability.ports import AuditUnavailableError
from adapter_verify.settings import (
    AccessSettings,
    ContractSettings,
    DatabaseSettings,
    GoldenSettings,
    IdempotencySettings,
)

EXIT_OK: Final = 0
EXIT_FAILED: Final = 1
EXIT_TOOL_ERROR: Final = 3

_DB_ERRORS: Final = (asyncpg.PostgresError, asyncpg.InterfaceError, OSError, TimeoutError)


def _fail(message: str) -> None:
    click.echo(f"error: {message}", err=True)
    sys.exit(EXIT_TOOL_ERROR)


def _database_settings() -> DatabaseSettings:
    try:
        return DatabaseSettings()  # read from the environment
    except ValidationError as exc:
        fields = ", ".join(
            ".".join(str(p) for p in err["loc"]) or "<settings>"
            for err in exc.errors(include_input=False)
        )
        _fail(f"invalid database settings ({fields}); see docs/SPEC.md section 4")
        raise  # unreachable; keeps type checkers satisfied


def _with_pool[T](work: Callable[[asyncpg.Pool], Awaitable[T]]) -> T:
    settings = _database_settings()

    async def run() -> T:
        pool = await composition.open_pool(settings)
        try:
            return await work(pool)
        finally:
            await pool.close()

    try:
        return asyncio.run(run())
    except (*_DB_ERRORS, AuditUnavailableError) as exc:
        _fail(f"database unavailable ({type(exc).__name__})")
        raise  # unreachable


@click.group()
def cli() -> None:
    """Execution and verification tooling for the MCP adapter (LLD sections 5-9)."""


@cli.group()
def db() -> None:
    """Database schema management."""


@db.command("migrate")
@click.option(
    "--migrations-dir",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    default=composition.MIGRATIONS_DIR,
    show_default=True,
    help="Directory of NNNN_name.sql files.",
)
def db_migrate(migrations_dir: Path) -> None:
    """Apply pending forward-only migrations."""
    try:
        applied = _with_pool(lambda pool: composition.run_migrations(pool, migrations_dir))
    except MigrationError as exc:
        _fail(str(exc))
        return
    click.echo("\n".join(applied) if applied else "up to date")


@cli.command("trace")
@click.option("--correlation-id", required=True, help="Correlation ID to reconstruct.")
@click.option(
    "--format", "fmt", type=click.Choice(["table", "json"]), default="table", show_default=True
)
def trace(correlation_id: str, fmt: str) -> None:
    """Print every recorded step for one correlation ID, oldest first (brief 8.9).

    Payload refs are printed as refs; resolving them needs payload-store permission
    (not available yet).
    """
    if not is_valid_correlation_id(correlation_id):
        _fail("correlation ID must match [A-Za-z0-9._:-]{1,128}")
    events = _with_pool(
        lambda pool: composition.trace_query(pool).by_correlation_id(correlation_id)
    )
    if not events:
        click.echo("no events recorded for this correlation ID", err=True)
        sys.exit(EXIT_FAILED)
    click.echo(render_table(events) if fmt == "table" else render_json(events))


def _access_settings() -> AccessSettings:
    try:
        return AccessSettings()
    except ValidationError as exc:
        fields = ", ".join(
            ".".join(str(p) for p in err["loc"]) or "<settings>"
            for err in exc.errors(include_input=False)
        )
        _fail(f"invalid access settings ({fields}); see docs/SPEC.md section 4")
        raise  # unreachable


def _access_config() -> tuple[list[PolicyDocument], ToolCatalog]:
    try:
        return composition.load_access_config(_access_settings())
    except (ConfigFileError, OSError) as exc:
        _fail(f"cannot load the tool catalog ({exc})")
        raise  # unreachable


@cli.group()
def policy() -> None:
    """Access policies (brief 9.2-9.3)."""


@policy.command("lint")
def policy_lint() -> None:
    """Check every policy file against the lint rules and the tool catalog. Exit 1 on findings."""
    documents, catalog = _access_config()
    findings = lint(documents, catalog)
    for f in findings:
        click.echo(f"{f.rule_id}\t{f.file_name}\t{f.detail}")
    if findings:
        sys.exit(EXIT_FAILED)
    click.echo(f"{len(documents)} policy file(s) clean")


@cli.group()
def access() -> None:
    """Access decisions (brief 9.4)."""


@access.command("explain")
@click.option("--agent", required=True, help="Agent ID.")
@click.option("--tool", required=True, help="Tool name.")
@click.option(
    "--version", "version", default=None, help="Tool version; default version if omitted."
)
def access_explain(agent: str, tool: str, version: str | None) -> None:
    """Print the decision for one agent, tool and version, and the rule that made it."""
    documents, catalog = _access_config()
    findings = lint(documents, catalog)
    if findings:
        _fail("policies fail lint; run `adapter-verify policy lint`")
    entry = catalog.resolve(tool, version)
    if entry is None:
        click.echo(f"denied\t{RULE_UNKNOWN_TOOL}\tNOT_AUTHORIZED\tno such tool version")
        sys.exit(EXIT_FAILED)
    policies = PolicySet(d.policy for d in documents if d.policy is not None)
    d = evaluate(policies, agent, entry.tool, entry.semver, entry.behavior)
    code = d.error_code.value if d.error_code else "-"
    click.echo(f"{'allowed' if d.allowed else 'denied'}\t{d.rule_id}\t{code}\t{d.reason}")
    click.echo(f"tool={entry.tool}@{entry.version} behavior={entry.behavior.value}")
    click.echo(f"policy_digest={d.policy_digest}")
    if not d.allowed:
        sys.exit(EXIT_FAILED)


def _contract_ci() -> ContractCi:
    try:
        return composition.contract_ci(ContractSettings())
    except ValidationError as exc:
        fields = ", ".join(
            ".".join(str(p) for p in e["loc"]) for e in exc.errors(include_input=False)
        )
        _fail(f"invalid contract settings ({fields}); see docs/SPEC.md section 4")
        raise  # unreachable


@cli.group()
def contract() -> None:
    """Contract testing (brief 5): upstream and canonical diffs, merge gate, impact report."""


@contract.command("check")
@click.option("--review-approved", is_flag=True, help="The PR carries contract-review-approved.")
@click.option(
    "--report", type=click.Path(dir_okay=False, path_type=Path), help="Write Markdown here."
)
def contract_check(*, review_approved: bool, report: Path | None) -> None:
    """Run all four checks. Exit 0 pass, 1 blocked, 2 review required, 3 unknown or tool error."""
    try:
        result, markdown = _contract_ci().check(
            composition.access_view(_access_settings()), review_approved=review_approved
        )
    except (ConfigFileError, OSError, ValueError) as exc:
        _fail(f"cannot run contract checks ({exc})")
        return
    click.echo(markdown)
    for target in (report, os.environ.get("GITHUB_STEP_SUMMARY")):
        if target:
            with Path(target).open("a", encoding="utf-8", newline="\n") as fh:
                fh.write(markdown)
    sys.exit(result.exit_code)


@contract.command("diff")
@click.option("--base", type=click.Path(exists=True, dir_okay=False, path_type=Path), required=True)
@click.option("--rev", type=click.Path(exists=True, dir_okay=False, path_type=Path), required=True)
@click.option(
    "--check",
    "kind",
    type=click.Choice([k.value for k in CheckKind]),
    default=CheckKind.UPSTREAM.value,
    show_default=True,
)
def contract_diff(base: Path, rev: Path, kind: str) -> None:
    """Diff two shape files and classify every change."""
    try:
        base_shape, rev_shape = read_shape(base), read_shape(rev)
        changes = diff(base_shape, rev_shape)
    except (ConfigFileError, ValueError) as exc:
        _fail(str(exc))
        return
    inferred = ProvenanceKind.INFERRED in {base_shape.provenance.kind, rev_shape.provenance.kind}
    findings = classify(changes, load_rulebook(), CheckKind(kind), inferred=inferred)
    for f in findings:
        click.echo(f"{f.classification.value}\t{f.rule_id}\t{f.operation}\t{f.path}\t{f.detail}")
    verdict = worst(f.classification for f in findings)
    click.echo(f"overall: {verdict.value}")
    sys.exit({"COMPATIBLE": 0, "BREAKING": 1, "REVIEW_REQUIRED": 2}.get(verdict.value, 3))


@contract.command("release")
@click.option("--tool", required=True)
@click.option("--version", required=True)
def contract_release(tool: str, version: str) -> None:
    """Record a contract version as released (commit the lock file in a reviewed PR)."""
    try:
        _contract_ci().release(tool, version)
    except (ReleaseError, ConfigFileError) as exc:
        _fail(str(exc))
    click.echo(f"released {tool}@{version}")


@cli.group()
def baseline() -> None:
    """Accepted upstream shapes (brief 5.9). CI never changes them."""


@baseline.command("extract")
@click.option("--source", "source_id", required=True)
def baseline_extract(source_id: str) -> None:
    """Print the shape of a source's current samples."""
    ci = _contract_ci()
    try:
        source = next(s for s in ci.sources() if s.source_id == source_id)
        click.echo(ci.extract(source).model_dump_json(indent=2))
    except StopIteration:
        _fail(f"unknown source {source_id}")
    except (ExtractionError, ValueError, ConfigFileError) as exc:
        _fail(str(exc))


@baseline.command("accept")
@click.option("--source", "source_id", required=True)
def baseline_accept(source_id: str) -> None:
    """Write a new baseline for review. Commit it in a PR; a CODEOWNER approves."""
    try:
        path = _contract_ci().accept_baseline(source_id)
    except KeyError:
        _fail(f"unknown source {source_id}")
        return
    except (ExtractionError, ValueError, ConfigFileError) as exc:
        _fail(str(exc))
        return
    click.echo(path)


@cli.group()
def rules() -> None:
    """Contract diff rules (brief 5.5)."""


@rules.command("list")
def rules_list() -> None:
    """Print every rule with its classifications."""
    for rule in load_rulebook().rules:
        up = rule.upstream.value if rule.upstream else "-"
        canon = rule.canonical.value if rule.canonical else "-"
        click.echo(f"{rule.id}\t{rule.direction.value}\tupstream={up}\tcanonical={canon}")


@cli.group()
def audit() -> None:
    """Tamper-evident audit trail (brief section 8.7)."""


@audit.command("anchor")
def audit_anchor() -> None:
    """Record the head of every chain that changed since its last anchor. Run on a schedule."""
    count = _with_pool(lambda pool: composition.audit_trail(pool).anchor())
    click.echo(f"anchored {count} chain(s)")


@audit.command("verify")
def audit_verify() -> None:
    """Verify every chain and every anchor. Exit 1 if anything was altered."""
    problems = _with_pool(lambda pool: composition.audit_trail(pool).verify())
    for p in problems:
        click.echo(f"{p.kind.value}\t{p.chain_id}\tseq={p.seq}")
    if problems:
        sys.exit(EXIT_FAILED)
    click.echo("audit trail intact")


def _idempotency_settings() -> IdempotencySettings:
    try:
        return IdempotencySettings()
    except ValidationError as exc:
        fields = ", ".join(
            ".".join(str(p) for p in e["loc"]) for e in exc.errors(include_input=False)
        )
        _fail(f"invalid idempotency settings ({fields}); see docs/SPEC.md section 4")
        raise  # unreachable


def _row(r: IdempotencyRecord) -> str:
    return "\t".join(
        [
            r.key.agent_id,
            r.key.tool,
            r.key.key,
            r.state.value,
            r.semantic_version,
            r.correlation_id,
            r.updated_at.isoformat(),
        ]
    )


@cli.group()
def idem() -> None:
    """Duplicate prevention (brief 7): lease sweeper, retention and reconciliation."""


@idem.command("sweep")
@click.option("--limit", type=click.IntRange(1, 100_000), default=1_000, show_default=True)
def idem_sweep(limit: int) -> None:
    """Mark RESERVED rows whose lease expired as UNKNOWN, audit and alert. Run on a schedule."""
    settings = _idempotency_settings()
    expired = _with_pool(
        lambda pool: composition.idempotency_maintenance(pool, settings).sweep(limit)
    )
    for record in expired:
        click.echo(_row(record))
    click.echo(f"{len(expired)} expired lease(s) marked UNKNOWN")


@idem.command("purge")
@click.option("--limit", type=click.IntRange(1, 1_000_000), default=10_000, show_default=True)
def idem_purge(limit: int) -> None:
    """Delete expired COMPLETED and FAILED_RETRYABLE rows. UNKNOWN rows are never deleted."""
    settings = _idempotency_settings()
    count = _with_pool(
        lambda pool: composition.idempotency_maintenance(pool, settings).purge(limit)
    )
    click.echo(f"purged {count} row(s)")


@idem.command("unknown")
@click.option("--limit", type=click.IntRange(1, 100_000), default=1_000, show_default=True)
def idem_unknown(limit: int) -> None:
    """List rows awaiting reconciliation, oldest first."""
    settings = _idempotency_settings()
    rows = _with_pool(
        lambda pool: composition.idempotency_maintenance(pool, settings).unknown(limit)
    )
    for record in rows:
        click.echo(_row(record))
    click.echo(f"{len(rows)} row(s) awaiting reconciliation", err=True)


@idem.command("resolve")
@click.option("--agent", required=True, help="Agent ID of the record.")
@click.option("--tool", required=True, help="Tool name of the record.")
@click.option("--key", required=True, help="Idempotency key of the record.")
@click.option(
    "--as",
    "resolution",
    type=click.Choice([r.value for r in Resolution]),
    required=True,
    help="completed: the effect happened. failed: it did not; the key becomes reusable.",
)
@click.option("--reason", required=True, help="Why; recorded on the audit trail.")
@click.option("--operator", required=True, help="Operator ID; the OS user is recorded too.")
def idem_resolve(  # noqa: PLR0913, PLR0917 - one parameter per option
    agent: str, tool: str, key: str, resolution: str, reason: str, operator: str
) -> None:
    """Settle one UNKNOWN record by hand. Exit 1 if no UNKNOWN record has this key."""
    if not (is_valid_key(key) and is_valid_key(operator)):
        _fail("key and operator must match [A-Za-z0-9._:-]{1,128}")
    if not reason.strip():
        _fail("--reason must not be empty")
    settings = _idempotency_settings()
    try:
        os_user = getpass.getuser()
    except (OSError, KeyError):
        os_user = "unknown"
    record = _with_pool(
        lambda pool: composition.idempotency_maintenance(pool, settings).resolve(
            RecordKey(agent_id=agent, tool=tool, key=key),
            Resolution(resolution),
            Operator(name=operator, os_user=os_user),
            reason,
        )
    )
    if record is None:
        click.echo("no UNKNOWN record with this agent, tool and key", err=True)
        sys.exit(EXIT_FAILED)
    click.echo(_row(record))


# --------------------------------------------------------------------------- golden tasks


EXIT_INCOMPLETE: Final = 2


def _golden_settings() -> GoldenSettings:
    try:
        return GoldenSettings()
    except ValidationError as exc:
        fields = ", ".join(
            ".".join(str(p) for p in e["loc"]) for e in exc.errors(include_input=False)
        )
        _fail(f"invalid golden settings ({fields}); see docs/SPEC.md section 4")
        raise  # unreachable


class _Golden:
    """Everything the golden commands read, loaded once."""

    def __init__(self) -> None:
        self.settings = _golden_settings()
        self.access = _access_settings()
        documents, self.catalog = _access_config()
        self.policies = PolicySet(d.policy for d in documents if d.policy is not None)
        try:
            self.workspace: Workspace = composition.golden_workspace(self.settings)
            self.contracts = composition.canonical_contracts(ContractSettings())
        except (ConfigFileError, OSError, ValueError) as exc:
            _fail(f"cannot load golden tasks ({exc})")
            raise  # unreachable
        self.layout = composition.golden_layout(self.settings, self.access)

    def plan(
        self,
        tasks: list[GoldenTask],
        git_sha: str | None,
        *,
        reasons: dict[str, list[str]] | None = None,
        previous: SuiteResults | None = None,
        suffix: str = "",
    ) -> SuitePlan:
        now = datetime.now(UTC)
        return SuitePlan(
            run_id=f"golden-{now:%Y%m%dT%H%M%SZ}{suffix}",
            git_sha=git_sha,
            tasks=tasks,
            agents=self.workspace.agents,
            digests=composition.golden_digests(self.workspace, self.policies),
            quarantine=self.workspace.quarantine,
            selected_because=reasons or {},
            previous=previous,
            token_budget=self.settings.token_budget,
        )

    def run(self, workspace: Workspace, plan: SuitePlan) -> SuiteResults:
        runner = composition.golden_runner(self.settings, self.access, workspace)
        try:
            return asyncio.run(runner.run(plan))
        except HarnessUnavailableError as exc:
            _fail(str(exc))
            raise  # unreachable

    def findings(self) -> list[golden_lint_rules.GoldenFinding]:
        return golden_lint_rules.lint(
            self.workspace.inputs, self.catalog, self.policies, self.contracts
        )


@cli.group()
def golden() -> None:
    """Golden-task regression (brief 6): lint, select, run, gate."""


@golden.command("lint")
def golden_lint() -> None:
    """Check task files, agent configs, tool definitions and fixtures. Exit 1 on findings."""
    g = _Golden()
    findings = g.findings()
    for f in findings:
        click.echo(f"{f.rule_id}\t{f.path}\t{f.detail}")
    if findings:
        sys.exit(EXIT_FAILED)
    click.echo(f"{len(g.workspace.tasks)} golden task(s) clean")


@golden.command("select")
@click.option("--changed-since", "base", required=True, help="Git ref to compare against.")
def golden_select(base: str) -> None:
    """Print the tasks a change affects, with the reason (brief 6.7)."""
    g = _Golden()
    try:
        changed = composition.git_repo(g.settings).changed_paths(base)
    except GitError as exc:
        _fail(str(exc))
        return
    for task_id, reasons in sorted(select(g.workspace.tasks, changed, g.layout).items()):
        click.echo(f"{task_id}\t{'; '.join(reasons)}")


@golden.command("run")
@click.option("--task", "task_ids", multiple=True, help="Run this task (repeatable).")
@click.option("--agent", "agents", multiple=True, help="Run this agent's tasks (repeatable).")
@click.option("--tool", "tools", multiple=True, help="Run tasks touching this tool (repeatable).")
@click.option("--all", "run_all", is_flag=True, help="Run every task.")
@click.option("--changed-since", "base", default=None, help="Run tasks affected since this ref.")
@click.option(
    "--previous",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    help="Earlier results, for quarantine recommendations.",
)
@click.option("--results", type=click.Path(dir_okay=False, path_type=Path), help="Results file.")
@click.option("--report", type=click.Path(dir_okay=False, path_type=Path), help="Markdown report.")
def golden_run(  # noqa: PLR0913, PLR0917 - one parameter per option
    task_ids: tuple[str, ...],
    agents: tuple[str, ...],
    tools: tuple[str, ...],
    run_all: bool,  # noqa: FBT001 - click flag
    base: str | None,
    previous: Path | None,
    results: Path | None,
    report: Path | None,
) -> None:
    """Run golden tasks. Exit 0 all pass, 1 any fail, 2 incomplete (not judged or model
    unavailable), 3 tool error."""
    g = _Golden()
    if g.findings():
        _fail("golden tasks fail lint; run `adapter-verify golden lint`")
    tasks = g.workspace.tasks
    reasons: dict[str, list[str]] = {}
    repo = composition.git_repo(g.settings)
    changed: list[str] = []
    if base is not None:
        try:
            changed = repo.changed_paths(base)
        except GitError as exc:
            _fail(str(exc))
        reasons = select(tasks, changed, g.layout)
    wanted = [
        t
        for t in tasks
        if run_all
        or t.task_id in task_ids
        or t.agent in agents
        or bool(t.tools() & set(tools))
        or t.task_id in reasons
    ]
    if not (run_all or task_ids or agents or tools or base):
        _fail("choose tasks: --all, --task, --agent, --tool or --changed-since")
    if not wanted:
        click.echo("no golden tasks selected")
        return
    try:
        prior = None if previous is None else read_results(previous)
    except ConfigFileError as exc:
        _fail(str(exc))
        return
    plan = g.plan(wanted, repo.head(), reasons=reasons, previous=prior)
    outcome = g.run(g.workspace, plan)
    target = results or g.settings.root / g.settings.results_dir / f"{outcome.run_id}.json"
    write_results(outcome, target)
    diffs = (
        {}
        if base is None
        else composition.golden_description_diffs(repo, base, g.workspace, g.layout, changed)
    )
    markdown = render(outcome, diffs)
    click.echo(markdown)
    for out in (report, os.environ.get("GITHUB_STEP_SUMMARY")):
        if out:
            with Path(out).open("a", encoding="utf-8", newline="\n") as fh:
                fh.write(markdown)
    click.echo(f"results: {target}", err=True)
    verdicts = {t.verdict for t in outcome.tasks if not t.quarantined}
    if TaskVerdict.FAIL in verdicts:
        sys.exit(EXIT_FAILED)
    if TaskVerdict.INCOMPLETE in verdicts:
        sys.exit(EXIT_INCOMPLETE)


@golden.command("gate")
@click.option("--tool", required=True, help="Tool being promoted.")
@click.option("--version", required=True, help="Version being promoted.")
@click.option(
    "--results",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    required=True,
    help="Results of the suite run to judge the promotion on.",
)
def golden_gate(tool: str, version: str, results: Path) -> None:
    """Promotion gate (brief 6.9). Exit 0 pass, 1 fail or stale, 2 blocked by quarantine."""
    g = _Golden()
    try:
        suite = read_results(results)
    except ConfigFileError as exc:
        _fail(str(exc))
        return
    default = g.catalog.resolve(tool, None)
    decision = gate(
        tool,
        version,
        tasks=g.workspace.tasks,
        digests=composition.golden_digests(g.workspace, g.policies),
        results=suite,
        quarantine=g.workspace.quarantine,
        default_version=None if default is None else default.version,
    )
    for line in decision.lines:
        click.echo(line)
    sys.exit(int(decision.exit))


@golden.command("calibrate")
def golden_calibrate() -> None:
    """Check the judge against the calibration cases (brief 6.5). Exit 1 on any miss."""
    g = _Golden()
    runner = composition.golden_runner(g.settings, g.access, g.workspace)
    misses = asyncio.run(runner.calibrate())
    if misses is None:
        _fail("no judge: set ADAPTER_GOLDEN_GEMINI_API_KEY (see .env.example)")
        return
    for miss in misses:
        if miss.judge_error is None:
            click.echo(f"MISSED\t{miss.case_id}")
        else:
            click.echo(f"ERROR\t{miss.case_id}\t{miss.judge_error}")
    if misses:
        sys.exit(EXIT_FAILED)
    click.echo(f"judge {g.settings.judge_model_id} classified every calibration case")


@golden.command("canaries")
def golden_canaries() -> None:
    """Apply each mutation canary and check the suite catches it (brief 6.8). Exit 1 unless all
    are caught."""
    g = _Golden()
    if g.findings():
        _fail("golden tasks fail lint; run `adapter-verify golden lint`")
    try:
        config = load_canaries(g.settings.root / g.settings.tasks_dir)
    except ConfigFileError as exc:
        _fail(f"canaries.yaml: {exc}")
        return
    tasks = g.workspace.tasks
    sha = composition.git_repo(g.settings).head()
    baseline = g.run(g.workspace, g.plan(tasks, sha, suffix="-baseline"))
    contracts = composition.contracts_by_tool(g.catalog, g.contracts)
    statuses: list[CanaryStatus] = []
    for kind in CanaryKind:
        mutation = mutate(kind, config, g.workspace.inputs, contracts)
        ids = affected(tasks, mutation.tools)
        chosen = [t for t in tasks if t.task_id in ids]
        mutated = (
            g.run(
                with_inputs(g.workspace, mutation.inputs),
                g.plan(chosen, sha, suffix=f"-{kind.value}"),
            )
            if chosen
            else None
        )
        result = status(ids, baseline, mutated)
        statuses.append(result)
        click.echo(f"{result.value.upper()}	{kind.value}	{', '.join(ids) or '-'}")
    if any(s is not CanaryStatus.CAUGHT for s in statuses):
        sys.exit(EXIT_FAILED)
