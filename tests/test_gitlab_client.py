"""Unit tests for GitLabClient — no live GitLab instance required.

``httpx.AsyncClient`` is monkeypatched with a small recording fake;
``httpx.Response`` objects are constructed directly (no real network),
mirroring pr-af's own test-file convention of stating, per test, the
contract/regression under test rather than testing implementation detail.

``GitHubComment``/``GitHubReview`` stand-ins are defined locally instead of
imported from the pinned `pr-af` git dependency, so these tests validate
GitLabClient's own translation logic without needing to install that
dependency over the network just to run `pytest`.
"""

from __future__ import annotations

import httpx
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


class _FakeReview:
    """Duck-typed stand-in for pr_af.schemas.output.GitHubReview."""

    def __init__(self, body: str = "", event: str = "COMMENT", comments=None):
        self.body = body
        self.event = event
        self.comments = comments or []


def _response(status: int, json_body: dict) -> httpx.Response:
    # httpx.Response.raise_for_status() requires a `request` to be attached
    # even on a 2xx status — confirmed empirically, not assumed.
    return httpx.Response(status, json=json_body, request=httpx.Request("POST", "https://gitlab.example.invalid"))


class _FakeAsyncClient:
    """Records every .get/.post call and returns the next queued response."""

    def __init__(self, responses: list[httpx.Response], calls: list[tuple]):
        self._responses = responses
        self._calls = calls

    async def __aenter__(self) -> _FakeAsyncClient:
        return self

    async def __aexit__(self, *exc) -> bool:
        return False

    async def get(self, url, headers=None, params=None):
        self._calls.append(("GET", url, None))
        return self._responses.pop(0)

    async def post(self, url, headers=None, json=None):
        self._calls.append(("POST", url, json))
        return self._responses.pop(0)


@pytest.fixture
def fake_httpx(monkeypatch):
    calls: list[tuple] = []
    responses: list[httpx.Response] = []

    def factory(*args, **kwargs):
        return _FakeAsyncClient(responses, calls)

    monkeypatch.setattr("pr_af_gitlab.gitlab.client.httpx.AsyncClient", factory)
    return responses, calls


# --- parse_mr_url ------------------------------------------------------------


@pytest.mark.parametrize(
    ("url", "expected_path", "expected_iid"),
    [
        ("https://gitlab.com/group/project/-/merge_requests/42", "group/project", 42),
        ("https://gitlab.example.com/g1/g2/g3/project/-/merge_requests/7", "g1/g2/g3/project", 7),
    ],
)
def test_parse_mr_url_handles_arbitrarily_nested_groups(url, expected_path, expected_iid):
    """GitLab project paths nest arbitrarily deep, unlike GitHub's fixed
    owner/repo — the regex must capture the whole path segment greedily."""
    path, iid = GitLabClient.parse_mr_url(url)
    assert path == expected_path
    assert iid == expected_iid


def test_parse_mr_url_rejects_non_mr_url():
    with pytest.raises(ValueError, match="Invalid GitLab MR URL"):
        GitLabClient.parse_mr_url("https://gitlab.com/group/project/-/issues/3")


# --- _encode_project_id -------------------------------------------------------


def test_encode_project_id_passes_through_numeric_ids():
    assert GitLabClient._encode_project_id(42) == "42"
    assert GitLabClient._encode_project_id("42") == "42"


def test_encode_project_id_url_encodes_namespaced_paths():
    assert GitLabClient._encode_project_id("group/subgroup/project") == "group%2Fsubgroup%2Fproject"


# --- post_review (Phase A / Phase B behavior) --------------------------------
# _build_position's field-by-field mapping has its own dedicated tests in
# test_position_mapping.py; these exercise the HTTP-level request/response
# flow around it.


@pytest.mark.asyncio
async def test_post_review_without_diff_refs_posts_summary_note_only(fake_httpx):
    """Phase A: no diff_refs supplied -> exactly one summary note, every
    comment recorded as skipped (not silently dropped)."""
    responses, calls = fake_httpx
    responses.append(_response(201, {"id": 111}))

    client = GitLabClient(token="t")
    review = _FakeReview(
        body="Found 2 issues",
        event="COMMENT",
        comments=[_FakeComment(path="a.py", line=1, side="RIGHT", body="issue 1")],
    )

    result = await client.post_review(project_id=42, mr_iid=7, review=review, diff_refs=None)

    assert result.summary_note_id == 111
    assert result.discussion_ids == []
    assert result.skipped_count == 1
    assert len(calls) == 1
    method, url, payload = calls[0]
    assert method == "POST"
    assert url.endswith("/projects/42/merge_requests/7/notes")
    assert "Found 2 issues" in payload["body"]


@pytest.mark.asyncio
async def test_post_review_with_diff_refs_posts_line_anchored_discussion(fake_httpx):
    """Phase B: diff_refs supplied -> the summary note plus one
    line-anchored discussion per comment."""
    responses, calls = fake_httpx
    responses.append(_response(201, {"id": 111}))  # summary note
    responses.append(_response(201, {"id": "disc-1"}))  # discussion

    client = GitLabClient(token="t")
    diff_refs = DiffRefs(base_sha="b", start_sha="s", head_sha="h")
    review = _FakeReview(
        body="Found 1 issue",
        event="REQUEST_CHANGES",
        comments=[_FakeComment(path="a.py", line=42, side="RIGHT", body="fix this")],
    )

    result = await client.post_review(project_id=42, mr_iid=7, review=review, diff_refs=diff_refs)

    assert result.summary_note_id == 111
    assert result.discussion_ids == ["disc-1"]
    assert result.skipped_count == 0

    _, url, payload = calls[1]
    assert url.endswith("/projects/42/merge_requests/7/discussions")
    assert payload["position"]["new_line"] == 42
    assert payload["position"]["base_sha"] == "b"


@pytest.mark.asyncio
async def test_post_review_falls_back_to_note_when_discussion_rejected(fake_httpx):
    """A finding GitLab's position API rejects (e.g. line outside the diff)
    falls back to a plain note rather than being silently dropped —
    PLAN.md §5.1's "never silently drop a finding" requirement."""
    responses, calls = fake_httpx
    responses.append(_response(201, {"id": 111}))  # summary note
    responses.append(_response(422, {"message": "line not part of the diff"}))  # discussion rejected
    responses.append(_response(201, {"id": 222}))  # fallback note

    client = GitLabClient(token="t")
    diff_refs = DiffRefs(base_sha="b", start_sha="s", head_sha="h")
    review = _FakeReview(
        body="Found 1 issue",
        event="COMMENT",
        comments=[_FakeComment(path="a.py", line=999, side="RIGHT", body="out of range")],
    )

    result = await client.post_review(project_id=42, mr_iid=7, review=review, diff_refs=diff_refs)

    assert result.discussion_ids == []
    assert result.fallback_note_ids == [222]
    assert result.skipped_count == 0
    assert len(calls) == 3


@pytest.mark.asyncio
async def test_post_review_summary_note_maps_event_to_readable_label(fake_httpx):
    """GitLab has no native review-event concept (PLAN.md §5.1) — the
    summary note's header must still convey it in readable form."""
    responses, calls = fake_httpx
    responses.append(_response(201, {"id": 111}))

    client = GitLabClient(token="t")
    review = _FakeReview(body="All good", event="APPROVE", comments=[])

    await client.post_review(project_id=1, mr_iid=1, review=review, diff_refs=None)

    _, _, payload = calls[0]
    assert "Approve" in payload["body"]


@pytest.mark.asyncio
async def test_post_review_never_raises_when_summary_note_succeeds_but_discussion_call_errors(fake_httpx):
    """A transport-level failure while posting one discussion must not
    abort the whole review — it should be recorded as a fallback attempt,
    not crash the CI job mid-run."""
    responses, calls = fake_httpx
    responses.append(_response(201, {"id": 111}))  # summary note
    responses.append(_response(500, {"message": "internal error"}))  # discussion 5xx
    responses.append(_response(201, {"id": 333}))  # fallback note succeeds

    client = GitLabClient(token="t")
    diff_refs = DiffRefs(base_sha="b", start_sha="s", head_sha="h")
    review = _FakeReview(
        body="Found 1 issue",
        event="COMMENT",
        comments=[_FakeComment(path="a.py", line=3, side="RIGHT", body="x")],
    )

    result = await client.post_review(project_id=1, mr_iid=1, review=review, diff_refs=diff_refs)

    assert result.fallback_note_ids == [333]
    assert result.skipped_count == 0
