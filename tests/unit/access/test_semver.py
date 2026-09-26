import pytest
from hypothesis import given
from hypothesis import strategies as st

from adapter_verify.access.domain.semver import Version, VersionRange

pytestmark = pytest.mark.unit

_versions = st.builds(
    Version,
    st.integers(0, 30),
    st.integers(0, 30),
    st.integers(0, 30),
    st.lists(st.sampled_from(["alpha", "beta", "rc", "1", "2", "x-y"]), max_size=2).map(tuple),
)


@given(v=_versions)
def test_parse_round_trips(v: Version) -> None:
    assert Version.parse(str(v)) == v


@given(a=_versions, b=_versions)
def test_ordering_is_total_and_consistent(a: Version, b: Version) -> None:
    assert (a < b) + (a == b) + (a > b) == 1


@pytest.mark.parametrize(
    "ordered",
    [
        [
            "1.0.0-alpha",
            "1.0.0-alpha.1",
            "1.0.0-alpha.beta",
            "1.0.0-beta",
            "1.0.0-beta.2",
            "1.0.0-beta.11",
            "1.0.0-rc.1",
            "1.0.0",
            "1.0.1",
            "1.1.0",
            "2.0.0",
        ]
    ],
)
def test_semver_spec_precedence(ordered: list[str]) -> None:
    parsed = [Version.parse(v) for v in ordered]
    assert parsed == sorted(parsed)


@pytest.mark.parametrize("text", ["1.0", "01.0.0", "1.0.0+build", "v1.0.0", "1.0.0-", ""])
def test_invalid_versions(text: str) -> None:
    with pytest.raises(ValueError, match="SemVer"):
        Version.parse(text)


def test_range_membership_and_prereleases() -> None:
    r = VersionRange.parse(">=1.0.0,<2.0.0")
    assert r.contains(Version.parse("1.0.0"))
    assert r.contains(Version.parse("1.9.9"))
    assert not r.contains(Version.parse("2.0.0"))
    assert not r.contains(Version.parse("0.9.0"))
    assert not r.contains(Version.parse("1.5.0-beta"))
    assert str(r) == ">=1.0.0,<2.0.0"


@pytest.mark.parametrize(
    ("text", "major"),
    [
        (">=1.0.0,<2.0.0", 1),
        (">=1.2.0,<1.5.0", 1),
        (">1.0.0,<=1.9.0", 1),
        ("==3.1.4", 3),
        (">=1.0.0", None),
        ("<2.0.0", None),
        (">=1.0.0,<3.0.0", None),
        (">=1.0.0,<=2.0.0", None),
    ],
)
def test_single_major(text: str, major: int | None) -> None:
    assert VersionRange.parse(text).single_major() == major


@pytest.mark.parametrize("text", ["~1.0.0", ">=1.0.0 <2.0.0", ">=1.0.0-rc.1", "", ">=1.0.0,"])
def test_invalid_ranges(text: str) -> None:
    with pytest.raises(ValueError, match=r"comparator|SemVer|pre-release"):
        VersionRange.parse(text)
