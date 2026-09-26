"""The sentinel plugin must actually catch leaks, or every test relying on it proves nothing."""

import pytest

# The inner runs leak sentinels on purpose, into sinks the outer scan also sees.
pytestmark = [pytest.mark.unit, pytest.mark.sentinel_exempt]

_LEAKY = """
import logging

from adapter_verify.observability.fakes import MemoryDiagnostics


def test_leaks_into_diagnostics():
    MemoryDiagnostics().internal_error("c", "SENTINEL-card-4111")


def test_leaks_into_logs():
    logging.getLogger("adapter_verify.x").info("value SENTINEL-card-4111")


def test_clean():
    MemoryDiagnostics().internal_error("c", "builtins.ValueError")
"""


_INI = """
[pytest]
asyncio_default_fixture_loop_scope = function
markers =
    sentinel_exempt: skip the sentinel scan
"""


def test_plugin_fails_tests_whose_sinks_contain_sentinels(pytester: pytest.Pytester) -> None:
    pytester.makeini(_INI)
    pytester.makepyfile(test_leaky=_LEAKY)
    result = pytester.runpytest_inprocess("-p", "tests.sentinels", "-p", "no:cacheprovider")
    result.assert_outcomes(passed=3, errors=2)
    result.stdout.fnmatch_lines(["*sentinel value reached an observable sink: MemoryDiagnostics*"])
    result.stdout.fnmatch_lines(["*sentinel value reached an observable sink: log records*"])


def test_exempt_marker_skips_the_scan(pytester: pytest.Pytester) -> None:
    pytester.makeini(_INI)
    pytester.makepyfile(
        test_exempt="""
import pytest
from adapter_verify.observability.fakes import MemoryDiagnostics

@pytest.mark.sentinel_exempt
def test_intentional():
    MemoryDiagnostics().internal_error("c", "SENTINEL-x")
"""
    )
    result = pytester.runpytest_inprocess("-p", "tests.sentinels", "-p", "no:cacheprovider")
    result.assert_outcomes(passed=1)
