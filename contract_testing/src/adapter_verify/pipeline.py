"""The stage chain behind ``ObservedEntry`` (brief §1.3, §3.5).

Authentication turns the RequestContext into a CallContext; the stages then run in the fixed
order given by the composition root. A stage may short-circuit; it can never reorder others.
"""

from collections.abc import Sequence

from pydantic import SecretStr

from adapter_kernel.context import CallContext, RequestContext
from adapter_kernel.pipeline import Next, Stage, ToolRequest, ToolResult
from adapter_verify.access.service import Authenticator


def chain(stages: Sequence[Stage], terminal: Next) -> Next:
    """Compose stages so ``stages[0]`` runs first and ``terminal`` last."""
    call_next = terminal
    for stage in reversed(stages):
        call_next = _bind(stage, call_next)
    return call_next


def _bind(stage: Stage, call_next: Next) -> Next:
    async def run(ctx: CallContext, request: ToolRequest) -> ToolResult:
        return await stage(ctx, request, call_next)

    return run


class AuthenticatedPipeline:
    """``Downstream`` for ``ObservedEntry``: authenticate, then run the stage chain."""

    def __init__(
        self, authenticator: Authenticator, stages: Sequence[Stage], terminal: Next
    ) -> None:
        self._authenticator = authenticator
        self.stage_names = tuple(s.name for s in stages)
        self._run = chain(stages, terminal)

    async def __call__(
        self, ctx: RequestContext, request: ToolRequest, credential: SecretStr | None
    ) -> ToolResult:
        authenticated = await self._authenticator.authenticate(ctx, credential)
        if isinstance(authenticated, ToolResult):
            return authenticated
        return await self._run(authenticated, request)
