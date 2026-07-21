"""Input-side schemas for the GitLab adapter — data fetched FROM GitLab.

Mirrors pr_af.schemas.input.GitHubPRData's shape where the concepts map,
per PLAN.md's "mirror don't fork" principle. Deliberately slimmer: the
review pipeline itself never consumes this (pr-af Mode 3 gets its diff
from the CI checkout, not from us) — GitLabMRData exists only to carry
the `diff_refs` triple a GitLab `position` object needs (PLAN.md §5.1)
and, optionally, MR metadata for a richer summary note (PLAN.md §12.3).
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class DiffRefs(BaseModel):
    """The three SHAs a GitLab `position` object needs.

    Authoritative source: ``GET /projects/:id/merge_requests/:iid`` ->
    ``diff_refs``. Deliberately NOT sourced from CI predefined variables —
    see PLAN.md §5.1 on why ``start_sha`` in particular has no reliable
    CI-variable equivalent.
    """

    base_sha: str
    start_sha: str
    head_sha: str


class GitLabMRData(BaseModel):
    """Merge request metadata fetched from the GitLab API.

    Mirrors ``GitHubPRData`` field-for-field where GitLab has an equivalent
    concept. GitHub's separate ``base_sha``/``head_sha`` strings are folded
    into ``diff_refs`` instead, since GitLab's position API needs all three
    SHAs (base/start/head) together, not just two.
    """

    project_id: int
    mr_iid: int
    title: str
    description: str = ""
    labels: list[str] = Field(default_factory=list)
    author: str = ""
    source_branch: str = ""
    target_branch: str = ""
    diff_refs: DiffRefs | None = None
