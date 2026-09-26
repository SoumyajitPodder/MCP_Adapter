"""Judge verdicts and calibration (brief §6.5). The judge itself is a port; its first adapter
arrives with M5b."""

from collections.abc import Sequence
from typing import Final

from pydantic import Field

from adapter_kernel.jsontypes import JsonObject
from adapter_verify.common.model import FrozenModel

PASS_SCORE: Final = 0.5


class JudgeVerdict(FrozenModel):
    """What a judge returns. Validated; an inconsistent verdict is JUDGE_ERROR, never a pass."""

    score: float = Field(ge=0.0, le=1.0, description="0 = fails the rubric, 1 = fully meets it.")
    passed: bool = Field(description="The judge's decision.")
    reasons: tuple[str, ...] = Field(min_length=1, description="Why, briefly.")

    @property
    def consistent(self) -> bool:
        return self.passed == (self.score >= PASS_SCORE)


class AnswerEvidence(FrozenModel):
    """Everything the judge sees: the question, the tool results shown to the agent, its answer."""

    prompt: str
    tool_results: tuple[JsonObject, ...] = Field(description="Agent-visible results, in order.")
    answer: str


class CalibrationCase(FrozenModel):
    """A transcript with a known verdict (``tests/golden_selftest/calibration/*.yaml``)."""

    case_id: str = Field(min_length=1)
    rubric: str = Field(min_length=1)
    evidence: AnswerEvidence
    expected_pass: bool


def calibration_misses(
    cases: Sequence[CalibrationCase], verdicts: Sequence[JudgeVerdict | None]
) -> list[str]:
    """Case IDs the judge got wrong. A missing or inconsistent verdict is a miss.

    A judge with no calibration cases is never calibrated.
    """
    if not cases:
        return ["<no calibration cases>"]
    return [
        case.case_id
        for case, verdict in zip(cases, verdicts, strict=True)
        if verdict is None or not verdict.consistent or verdict.passed != case.expected_pass
    ]
