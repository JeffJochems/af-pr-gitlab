"""Tests for GitLabClient._build_position — the field-by-field GitHubComment
-> GitLab `position` object mapping (PLAN.md §5.1), isolated from the HTTP
layer (see test_gitlab_client.py for the request/response-level behavior).

GitHubComment stand-ins are defined locally rather than imported from the
pinned `pr-af` git dependency — see test_gitlab_client.py's module
docstring for why.
"""

from __future__ import annotations

import pytest

from pr_af_gitlab.gitlab.client import GitLabClient
from pr_af_gitlab.schemas.input import DiffRefs


class _FakeComment:
    """Duck-typed stand-in for pr_af.schemas.output.GitHubComment."""

    def __init__(self, path: str = "", line: int = 0, side: str = "RIGHT", body: str = ""):
        self.path = path
        self.line = line
        self.side = side
        self.body = body


def test_build_position_right_side_sets_new_line_only():
    """An added/context-line comment (side=RIGHT) maps to new_line, with
    old_line left unset — GitLab rejects positions with both set for a
    pure addition."""
    client = GitLabClient(token="t")
    diff_refs = DiffRefs(base_sha="base", start_sha="start", head_sha="head")
    comment = _FakeComment(path="src/app.py", line=10, side="RIGHT")

    position = client._build_position(comment, diff_refs)

    assert position is not None
    assert position.new_line == 10
    assert position.old_line is None
    assert position.new_path == "src/app.py"
    assert position.old_path == "src/app.py"
    assert position.base_sha == "base"
    assert position.start_sha == "start"
    assert position.head_sha == "head"
    assert position.position_type == "text"


def test_build_position_left_side_sets_old_line_only():
    """A removed-line comment (side=LEFT) maps to old_line, the mirror
    image of the RIGHT-side case."""
    client = GitLabClient(token="t")
    diff_refs = DiffRefs(base_sha="base", start_sha="start", head_sha="head")
    comment = _FakeComment(path="src/app.py", line=5, side="LEFT")

    position = client._build_position(comment, diff_refs)

    assert position.old_line == 5
    assert position.new_line is None


def test_build_position_uses_renamed_paths_for_old_path():
    """GitHubComment carries no rename info — the caller supplies it
    separately (renamed_paths), keyed by the finding's current path. This
    is the one piece of PLAN.md §5.1's rename handling that's actually
    wireable: ReviewResult itself exposes no rename data (see
    scripts/ci_runner.py's "KNOWN GAP" comment), so renamed_paths is empty
    in production today, but the mapping logic itself is tested here
    against a future path once that data is available."""
    client = GitLabClient(token="t")
    diff_refs = DiffRefs(base_sha="base", start_sha="start", head_sha="head")
    comment = _FakeComment(path="src/new_name.py", line=3, side="RIGHT")

    position = client._build_position(comment, diff_refs, renamed_paths={"src/new_name.py": "src/old_name.py"})

    assert position.new_path == "src/new_name.py"
    assert position.old_path == "src/old_name.py"


def test_build_position_defaults_old_path_to_new_path_without_rename_info():
    client = GitLabClient(token="t")
    diff_refs = DiffRefs(base_sha="base", start_sha="start", head_sha="head")
    comment = _FakeComment(path="src/app.py", line=3, side="RIGHT")

    position = client._build_position(comment, diff_refs)

    assert position.old_path == position.new_path == "src/app.py"


@pytest.mark.parametrize(("path", "line"), [("", 5), ("src/app.py", 0), ("src/app.py", -1)])
def test_build_position_returns_none_when_unanchorable(path, line):
    """No path or a non-positive line number can't be anchored to a diff
    line — the caller must fall back to a plain note, never crash."""
    client = GitLabClient(token="t")
    diff_refs = DiffRefs(base_sha="base", start_sha="start", head_sha="head")
    comment = _FakeComment(path=path, line=line, side="RIGHT")

    assert client._build_position(comment, diff_refs) is None
