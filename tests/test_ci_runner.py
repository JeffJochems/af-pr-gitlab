"""Tests for scripts/ci_runner.py.

Covers the pieces testable without the pinned `pr-af` git dependency
installed and without a live AgentField control plane or GitLab instance:
the result-envelope unwrapping (an external, unpinned contract per
PLAN.md §13 — mirrors pr-af's own defensive `_unwrap` convention rather
than assuming one exact shape) and the merge-request-pipeline guard in
`_post_to_gitlab`.

`_post_to_gitlab`'s happy path (constructing a real `GitHubReview` and
calling `GitLabClient.post_review`) is deliberately NOT re-tested here —
that translation logic already has full coverage in test_gitlab_client.py
and test_position_mapping.py against duck-typed stand-ins; re-testing it
here would need the real `pr_af` package installed (a network-fetched git
dependency) just to import a two-field Pydantic model. Covered instead by
PLAN.md §10's manual end-to-end test plan.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import ci_runner  # noqa: E402

# --- _unwrap_result ------------------------------------------------------


def test_unwrap_result_returns_output_key_when_present():
    payload = {"output": {"review": {"body": "x"}}, "status": "succeeded"}
    assert ci_runner._unwrap_result(payload) == {"review": {"body": "x"}}


def test_unwrap_result_returns_result_key_when_output_absent():
    payload = {"result": {"review": {"body": "y"}}}
    assert ci_runner._unwrap_result(payload) == {"review": {"body": "y"}}


def test_unwrap_result_returns_payload_unchanged_when_neither_key_present():
    """Covers the possibility that the control plane returns the
    ReviewResult flat, with no output/result envelope at all — an
    unverified, version-dependent contract (PLAN.md §13)."""
    payload = {"review": {"body": "z"}, "findings": []}
    assert ci_runner._unwrap_result(payload) == payload


def test_unwrap_result_prefers_output_over_result_when_both_present():
    payload = {"output": {"marker": "output"}, "result": {"marker": "result"}}
    assert ci_runner._unwrap_result(payload) == {"marker": "output"}


# --- _post_to_gitlab's merge-request-pipeline guard -----------------------


@pytest.mark.asyncio
async def test_post_to_gitlab_returns_early_without_project_id(monkeypatch, capsys):
    """No CI_PROJECT_ID/CI_MERGE_REQUEST_IID -> not a merge-request
    pipeline -> return without attempting to post, and without requiring
    the pinned pr-af dependency to be importable (see the module
    docstring above and ci_runner.py's import-ordering comment)."""
    monkeypatch.delenv("CI_PROJECT_ID", raising=False)
    monkeypatch.delenv("CI_MERGE_REQUEST_IID", raising=False)

    await ci_runner._post_to_gitlab({"review": {"body": "unused"}})

    assert "nothing to post" in capsys.readouterr().out


@pytest.mark.asyncio
async def test_post_to_gitlab_returns_early_without_mr_iid(monkeypatch, capsys):
    monkeypatch.setenv("CI_PROJECT_ID", "42")
    monkeypatch.delenv("CI_MERGE_REQUEST_IID", raising=False)

    await ci_runner._post_to_gitlab({"review": {"body": "unused"}})

    assert "nothing to post" in capsys.readouterr().out
