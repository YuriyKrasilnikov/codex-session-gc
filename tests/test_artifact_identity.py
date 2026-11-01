# Copyright (c) 2026

"""Release identities reject incomplete and ambiguous inputs."""

import pytest

from codex_session_gc.artifacts import ArtifactIdentity, SourceIdentity


def test_exact_identity() -> None:
    """Accept a complete digest, size, commit and ref."""
    if ArtifactIdentity("a" * 64, 0).size != 0:
        raise AssertionError
    if SourceIdentity("b" * 40, "refs/tags/v1").ref != "refs/tags/v1":
        raise AssertionError


@pytest.mark.parametrize(("digest", "size"), [("a" * 63, 1), ("A" * 64, 1), ("a" * 64, -1)])
def test_bad_artifact(digest: str, size: int) -> None:
    """Invalid content identities cannot be constructed."""
    with pytest.raises(ValueError, match=r".+"):
        ArtifactIdentity(digest, size)


@pytest.mark.parametrize(
    ("commit", "ref"), [("a" * 39, "refs/tags/v1"), ("a" * 40, "v1"), ("a" * 40, "refs/tags/a..b")]
)
def test_bad_source(commit: str, ref: str) -> None:
    """Source identity needs a full commit and ref."""
    with pytest.raises(ValueError, match=r".+"):
        SourceIdentity(commit, ref)
