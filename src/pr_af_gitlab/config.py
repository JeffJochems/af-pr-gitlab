"""Configuration for the GitLab adapter.

Mirrors pr_af.config's pattern: pydantic BaseModel + Field(default_factory=
lambda: os.getenv(...)) per field, one from_env classmethod. All
GitLab-specific knobs live here; nothing GitLab-specific leaks into
pr-af's own config (which the adapter never modifies).
"""

from __future__ import annotations

import os

from pydantic import BaseModel, Field


class GitLabIntegrationConfig(BaseModel):
    """Everything ``GitLabClient`` needs to talk to a GitLab instance.

    Field defaults read GitLab CI predefined variables first (so a bare
    ``.gitlab-ci.yml`` job needs almost no explicit configuration), falling
    back to adapter-specific ``PR_AF_GITLAB_*`` env vars for anything CI
    doesn't predefine — the token, above all, since GitLab has no
    predefined variable that reliably has permission to post discussions
    (see PLAN.md §7.1).
    """

    api_url: str = Field(
        default_factory=lambda: os.getenv("CI_API_V4_URL")
        or os.getenv("PR_AF_GITLAB_API_URL", "https://gitlab.com/api/v4")
    )
    project_id: str = Field(default_factory=lambda: os.getenv("CI_PROJECT_ID", ""))
    mr_iid: str = Field(default_factory=lambda: os.getenv("CI_MERGE_REQUEST_IID", ""))

    # PLAN.md §7.1: a dedicated Project/Group Access Token is required, not
    # just recommended — confirmed against GitLab's own docs (CI/CD job token
    # docs), CI_JOB_TOKEN only ever gets read (GET) access to the Notes API,
    # never POST, so it cannot post discussions or notes under any GitLab
    # version/config. The CI_JOB_TOKEN fallback below still has a legitimate
    # use: it's sufficient for fetch_mr's read-only GET call when no dedicated
    # token is configured. PR_AF_GITLAB_TOKEN wins if set.
    token: str = Field(
        default_factory=lambda: os.getenv("PR_AF_GITLAB_TOKEN") or os.getenv("CI_JOB_TOKEN", "")
    )
    # "PRIVATE-TOKEN" for a PAT / Project / Group Access Token — the only
    # viable path for posting (see above). "JOB-TOKEN" is offered for
    # completeness/read-only use, not as a posting alternative.
    token_header: str = Field(default_factory=lambda: os.getenv("PR_AF_GITLAB_TOKEN_HEADER", "PRIVATE-TOKEN"))

    min_severity: str = Field(default_factory=lambda: os.getenv("PR_AF_GITLAB_MIN_SEVERITY", "nitpick"))
    max_discussions: int = Field(default_factory=lambda: int(os.getenv("PR_AF_GITLAB_MAX_DISCUSSIONS", "25")))

    # Phase A/B switch: when False, always post a single summary note and
    # skip position-object discussions entirely (PLAN.md §5.2, §9 Phase A).
    line_anchored_discussions: bool = Field(
        default_factory=lambda: os.getenv("PR_AF_GITLAB_LINE_ANCHORED", "1").lower() not in ("0", "false", "no")
    )

    @classmethod
    def from_env(cls) -> GitLabIntegrationConfig:
        return cls()
