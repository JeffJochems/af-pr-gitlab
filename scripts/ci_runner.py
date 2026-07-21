#!/usr/bin/env python3
"""CI Runner for pr-af-gitlab.

Fires an async execution to the AgentField Control Plane, polls until
completion, fetches the full ReviewResult, and posts it to the GitLab MR
via GitLabClient. Mirrors pr-af's own scripts/ci_runner.py shape (fire ->
poll -> exit 0/1) for the first half; the result-fetch-and-post step is
new, because pr-af's own container posts to GitHub itself in Mode 1 but
Mode 3 (repo_path/base_ref/head_ref, what this adapter always uses) has
no GitHub call to make and thus nothing to mirror — the CI runner owns
posting instead (see PLAN.md §2.1, §4).
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
import urllib.error
import urllib.request

CP_URL = os.environ.get("AGENTFIELD_SERVER", "http://localhost:8080")
POLL_INTERVAL_SECONDS = 30


def _post_json(url: str, payload: dict) -> dict:
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req) as response:
        return json.loads(response.read().decode())


def _get_json(url: str) -> dict:
    with urllib.request.urlopen(url) as response:
        return json.loads(response.read().decode())


def _unwrap_result(payload: dict) -> dict:
    """The control plane's execution-result envelope shape is an external,
    unpinned contract (the `agentfield` package, not `pr-af` itself — see
    PLAN.md §13). Mirror pr-af's own defensive `_unwrap` convention
    (orchestrator.py) rather than assume one exact shape."""
    if isinstance(payload, dict):
        if isinstance(payload.get("output"), dict):
            return payload["output"]
        if isinstance(payload.get("result"), dict):
            return payload["result"]
    return payload


def main() -> None:
    # NOTE: this is NOT $CI_PROJECT_DIR. pr-af runs in its own container,
    # brought up via `docker compose` from inside the CI job (typically
    # against a `docker:dind` service) — that container does not share the
    # job's filesystem. `repo_path` must be a path *inside the pr-af
    # container*, populated by the CI template's `docker compose cp
    # "$CI_PROJECT_DIR" pr-af:$PR_AF_CONTAINER_REPO_PATH` step before this
    # script runs (see templates/pr-af-review.gitlab-ci.yml).
    repo_path = os.environ.get("PR_AF_CONTAINER_REPO_PATH", "/workspaces/ci-checkout")
    base_ref = os.environ.get("PR_AF_BASE_REF") or os.environ.get("CI_MERGE_REQUEST_DIFF_BASE_SHA")
    head_ref = os.environ.get("PR_AF_HEAD_REF") or os.environ.get("CI_COMMIT_SHA")
    depth = os.environ.get("PR_AF_DEPTH", "auto")

    if not base_ref or not head_ref:
        print(
            "Error: could not resolve base_ref/head_ref. Expected "
            "CI_MERGE_REQUEST_DIFF_BASE_SHA/CI_COMMIT_SHA (set on merge-request "
            "pipelines) or PR_AF_BASE_REF/PR_AF_HEAD_REF overrides."
        )
        sys.exit(1)

    print(
        f"[CI] Initiating PR-AF review: repo_path={repo_path} "
        f"base={base_ref[:12]} head={head_ref[:12]} depth={depth}"
    )

    payload = {
        "input": {
            "repo_path": repo_path,
            "base_ref": base_ref,
            "head_ref": head_ref,
            "depth": depth,
            # Mode 3 (repo_path set, no pr_url) never triggers pr-af's own
            # GitHubClient.post_review call regardless of dry_run (see
            # orchestrator.py's `if ... and self.input.pr_url` guard) — set
            # explicitly anyway so a future upstream change can't silently
            # start posting somewhere on our behalf (PLAN.md §4).
            "dry_run": True,
            "output_format": "markdown",
        }
    }

    try:
        res_data = _post_json(f"{CP_URL}/api/v1/execute/async/pr-af.review", payload)
    except urllib.error.URLError as e:
        print(f"Error triggering review: {e}")
        sys.exit(1)

    exec_id = res_data.get("execution_id")
    if not exec_id:
        print(f"Error: failed to get execution_id (response: {res_data})")
        sys.exit(1)
    print(f"[CI] Review dispatched. Execution ID: {exec_id}")

    print("[CI] Polling for completion (this may take 30-60 minutes)...")
    start_time = time.time()
    while True:
        time.sleep(POLL_INTERVAL_SECONDS)
        elapsed_min = (time.time() - start_time) / 60
        try:
            status_data = _get_json(f"{CP_URL}/api/ui/v1/executions/{exec_id}/details")
        except urllib.error.URLError as e:
            print(f"[{elapsed_min:.1f}m] Warning: could not reach Control Plane API: {e}")
            continue

        status = status_data.get("status")
        print(f"[{elapsed_min:.1f}m] Status: {status}")
        if status == "succeeded":
            break
        if status in ("failed", "cancelled"):
            print(f"[CI] Review ended with status: {status}")
            print(f"Error details: {status_data.get('error', 'None')}")
            sys.exit(1)

    print("[CI] Review succeeded. Fetching full result...")
    try:
        result_data = _get_json(f"{CP_URL}/api/v1/executions/{exec_id}")
    except urllib.error.URLError as e:
        print(f"Error fetching execution result: {e}")
        sys.exit(1)

    review_result = _unwrap_result(result_data)
    try:
        asyncio.run(_post_to_gitlab(review_result))
    except Exception as exc:  # noqa: BLE001 — a posting failure must not look like a review failure
        print(f"[CI] Warning: review succeeded but posting to GitLab failed: {exc}")
        sys.exit(1)

    print("\n[CI] Review posted to GitLab. Done.")


async def _post_to_gitlab(review_result: dict) -> None:
    # Imported lazily: the polling loop above is the long-running, stdlib-only
    # part of this script and shouldn't need pr-af/pr-af-gitlab importable
    # until there's an actual result to post.
    from pr_af_gitlab.config import GitLabIntegrationConfig

    config = GitLabIntegrationConfig.from_env()
    if not config.project_id or not config.mr_iid:
        print(
            "[CI] CI_PROJECT_ID/CI_MERGE_REQUEST_IID not set — this isn't a "
            "merge-request pipeline, nothing to post."
        )
        return

    # Deferred further still: only needed once we know there's actually a
    # merge request to post to, so the guard above never requires the
    # pinned pr-af dependency to be import-able.
    from pr_af.schemas.output import GitHubReview  # pinned dependency — see pyproject.toml

    from pr_af_gitlab.gitlab.client import GitLabClient

    review = GitHubReview.model_validate(review_result["review"])
    client = GitLabClient(token=config.token, api_url=config.api_url, token_header=config.token_header)

    diff_refs = None
    if config.line_anchored_discussions:
        mr_data = await client.fetch_mr(config.project_id, int(config.mr_iid))
        diff_refs = mr_data.diff_refs

    # KNOWN GAP (see PLAN.md's testing/risk notes): ReviewResult carries no
    # rename information anywhere (ReviewMetadata's intake/anatomy payloads
    # don't include ChangedFile.previous_path) — renamed-file positions fall
    # back to old_path == new_path until a future pr-af release exposes it.
    result = await client.post_review(
        project_id=config.project_id,
        mr_iid=int(config.mr_iid),
        review=review,
        diff_refs=diff_refs,
    )
    print(
        f"[CI] Posted: summary_note={result.summary_note_id}, "
        f"{len(result.discussion_ids)} discussion(s), "
        f"{len(result.fallback_note_ids)} fallback note(s), "
        f"{result.skipped_count} skipped"
    )


if __name__ == "__main__":
    main()
