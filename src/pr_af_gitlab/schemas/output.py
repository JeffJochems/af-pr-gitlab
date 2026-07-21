"""Output-side schemas for the GitLab adapter — what we send TO GitLab.

Mirrors pr_af.schemas.output's GitHubComment/GitHubReview shape (imported
from the pinned pr-af dependency, not redefined — see pyproject.toml) as
the INPUT we receive from the control plane. These schemas are the other
half: the GitLab-native shapes we translate that input into. See
PLAN.md §5.1 for the full field-by-field position mapping.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class GitLabPosition(BaseModel):
    """The GitLab Discussions API ``position`` object for an inline text
    comment (``position_type=text``).

    ``base_sha``/``start_sha``/``head_sha`` come from the MR's ``diff_refs``
    (``GitLabMRData.diff_refs``), never from CI predefined variables.
    ``old_line``/``new_line`` are mutually exclusive in practice: a comment
    on an added/context line ("RIGHT" side) sets ``new_line`` and leaves
    ``old_line`` unset; a comment on a removed line ("LEFT" side) is the
    reverse.
    """

    position_type: str = "text"
    base_sha: str
    start_sha: str
    head_sha: str
    old_path: str
    new_path: str
    old_line: int | None = None
    new_line: int | None = None


class GitLabDiscussion(BaseModel):
    """A single line-anchored discussion, posted via
    ``POST /projects/:id/merge_requests/:iid/discussions``."""

    body: str
    position: GitLabPosition


class GitLabNote(BaseModel):
    """A plain MR-level note (no ``position``) — used for the Phase A
    summary note and as the fallback for findings that can't be cleanly
    anchored to a diff line (PLAN.md §5.1)."""

    body: str


class GitLabPostResult(BaseModel):
    """What ``GitLabClient.post_review`` returns: a record of what was
    actually posted, for logging and tests — mirrors the return shape of
    ``GitHubClient.post_review`` (a plain dict of the API response) closely
    enough to log the same way, but aggregates across the several GitLab
    API calls one review's worth of findings requires."""

    summary_note_id: int | None = None
    discussion_ids: list[str] = Field(default_factory=list)
    fallback_note_ids: list[int] = Field(default_factory=list)
    skipped_count: int = 0
