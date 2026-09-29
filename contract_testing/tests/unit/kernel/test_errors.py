import pytest
from pydantic import ValidationError

from adapter_kernel.errors import ERROR_SPECS, AdapterError, ErrorCode, RetryPolicy

pytestmark = pytest.mark.unit


def test_every_error_code_has_a_spec() -> None:
    assert set(ERROR_SPECS) == set(ErrorCode)


def test_error_specs_are_read_only() -> None:
    with pytest.raises(TypeError):
        ERROR_SPECS[ErrorCode.INTERNAL] = ERROR_SPECS[ErrorCode.INVALID_INPUT]  # type: ignore[index]


@pytest.mark.parametrize("code", list(ErrorCode))
def test_agent_messages_contain_no_placeholders(code: ErrorCode) -> None:
    message = ERROR_SPECS[code].agent_message
    assert "{" not in message
    assert "%" not in message


def test_only_duplicate_in_progress_asks_for_a_delay() -> None:
    delayed = {c for c, s in ERROR_SPECS.items() if s.retry is RetryPolicy.AFTER_DELAY}
    assert delayed == {ErrorCode.DUPLICATE_IN_PROGRESS}


def test_reconciliation_pending_is_never_retryable() -> None:
    assert ERROR_SPECS[ErrorCode.RECONCILIATION_PENDING].retry is RetryPolicy.NEVER


def test_delay_required_for_after_delay_codes() -> None:
    with pytest.raises(ValidationError, match="retry_after_ms"):
        AdapterError(code=ErrorCode.DUPLICATE_IN_PROGRESS)
    error = AdapterError(code=ErrorCode.DUPLICATE_IN_PROGRESS, retry_after_ms=500)
    assert error.spec.retry is RetryPolicy.AFTER_DELAY


def test_delay_rejected_for_other_codes() -> None:
    with pytest.raises(ValidationError, match="retry_after_ms"):
        AdapterError(code=ErrorCode.INTERNAL, retry_after_ms=500)


def test_delay_must_be_positive() -> None:
    with pytest.raises(ValidationError):
        AdapterError(code=ErrorCode.DUPLICATE_IN_PROGRESS, retry_after_ms=0)
