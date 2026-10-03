"""What the reference agents and judges share, whichever model API they use (D-083, D-095)."""

import hashlib
import json
from typing import Any, Final

import rfc8785

from adapter_kernel.errors import ERROR_SPECS
from adapter_kernel.jsontypes import JsonObject
from adapter_kernel.pipeline import ToolFailure, ToolSuccess
from adapter_verify.golden.domain.judge import AnswerEvidence

REFERENCE_HARNESS: Final = "reference"
"""Harness kind of the in-repo reference agent, on whichever provider ``agent.yaml`` names."""

VERDICT_SCHEMA: Final[dict[str, Any]] = {
    "type": "object",
    "properties": {
        "score": {"type": "number", "minimum": 0, "maximum": 1},
        "passed": {"type": "boolean"},
        "reasons": {"type": "array", "items": {"type": "string"}, "minItems": 1},
    },
    "required": ["score", "passed", "reasons"],
}
JUDGE_INSTRUCTIONS: Final = (
    "You grade one answer given by a customer-support agent. You receive the customer's "
    "question, the tool results the agent saw, a rubric, and the agent's final answer. "
    "Decide whether the answer meets every point of the rubric and is supported by the tool "
    "results. An answer that misses or violates any point of the rubric fails, however good "
    "the rest is: give it a score below 0.5. Return JSON only: score from 0 (fails) to 1 "
    "(fully meets), passed true exactly when score >= 0.5, and one short reason per point "
    "that decided it."
)


def judge_request(rubric: str, evidence: AnswerEvidence) -> str:
    return json.dumps(
        {
            "question": evidence.prompt,
            "tool_results": list(evidence.tool_results),
            "rubric": rubric,
            "answer": evidence.answer,
        },
        indent=2,
        sort_keys=True,
    )


def tool_payload(outcome: ToolSuccess | ToolFailure) -> dict[str, Any]:
    """What the model sees as the function result: the agent-facing view only."""
    if isinstance(outcome, ToolSuccess):
        return {"result": outcome.content}
    error: dict[str, Any] = {
        "code": outcome.error.code.value,
        "message": ERROR_SPECS[outcome.error.code].agent_message,
    }
    if outcome.error.retry_after_ms is not None:
        error["retry_after_ms"] = outcome.error.retry_after_ms
    return {"error": error}


def idempotency_key(tool: str, arguments: JsonObject) -> str:
    """The key an orchestrator would send: stable for the same call, so a retry can't double
    an effect (§7). Read-only tools ignore it."""
    try:
        canonical = rfc8785.dumps({"tool": tool, "arguments": arguments})
    except rfc8785.CanonicalizationError:
        canonical = repr((tool, sorted(arguments))).encode()
    return f"ref-{hashlib.sha256(canonical).hexdigest()[:40]}"
