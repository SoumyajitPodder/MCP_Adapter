"""``adapter-verify`` entry points.

Exit codes: 0 success, 1 check failed or nothing found, 3 configuration or tool error
(matches the contract-CI convention in brief §5.10 where it overlaps).

Errors are reported without echoing inputs: settings errors can contain the database DSN,
and driver errors can quote data.
"""

import asyncio
import os
import sys
from collections.abc import Awaitable, Callable
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
from adapter_verify.observability.domain.ids import is_valid_correlation_id
from adapter_verify.observability.domain.trace import render_json, render_table
from adapter_verify.observability.ports import AuditUnavailableError
from adapter_verify.settings import AccessSettings, ContractSettings, DatabaseSettings

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
