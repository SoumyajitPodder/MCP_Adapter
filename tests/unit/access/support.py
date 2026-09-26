"""Synthetic catalog and policies shared by access tests. Includes a mutating tool that the
pilot catalog does not have, so scope rules are exercised."""

from adapter_kernel.tooldef import Behavior
from adapter_verify.access.domain.lint import PolicyDocument
from adapter_verify.access.domain.policy import CatalogEntry, Grant, Policy, Scope, ToolCatalog

CATALOG = ToolCatalog(
    tools=(
        CatalogEntry(tool="order.get", version="1.2.0", behavior=Behavior.READ_ONLY, default=True),
        CatalogEntry(tool="order.get", version="2.0.0", behavior=Behavior.READ_ONLY),
        CatalogEntry(
            tool="service.get", version="1.0.0", behavior=Behavior.READ_ONLY, default=True
        ),
        CatalogEntry(
            tool="order.cancel", version="1.0.0", behavior=Behavior.MUTATING, default=True
        ),
    )
)


def policy(
    agent: str = "reader",
    scope: Scope = Scope.READ,
    owner: str = "team",
    grants: tuple[tuple[str, str], ...] = (("order.get", ">=1.0.0,<2.0.0"),),
) -> Policy:
    return Policy(
        agent_id=agent,
        owner=owner,
        scope=scope,
        grants=tuple(Grant(tool=t, versions=v) for t, v in grants),
    )


def document(p: Policy, file_name: str | None = None) -> PolicyDocument:
    return PolicyDocument(file_name=file_name or f"{p.agent_id}.yaml", policy=p, load_error=None)


READER = policy()
WRITER = policy(
    agent="writer",
    scope=Scope.WRITE,
    grants=(("order.get", ">=1.0.0,<2.0.0"), ("order.cancel", ">=1.0.0,<2.0.0")),
)
