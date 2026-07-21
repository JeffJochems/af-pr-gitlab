"""GitLab counterpart of ``pr_af.github.client.GitHubClient``.

Mirrors its method shapes 1:1 where GitLab has an equivalent concept
(``parse_*_url``, ``post_review``, ``clone_repo``); ``fetch_mr`` stands in
for ``fetch_pr`` but is deliberately NOT on pr-af-gitlab's critical CI path
(Mode 3 supplies the diff from the CI checkout already) — it exists to
fetch ``diff_refs`` for the position mapping and, optionally, MR metadata
for a richer summary note. Same async style, same ``httpx`` usage pattern,
same print-based logging convention as upstream. See PLAN.md §5 for the
full method-by-method mapping and every documented GitHub<->GitLab
divergence (no GitHub-App-style installation auth, no native "review
event" concept, arbitrarily-nested project paths instead of owner/repo).
"""

from __future__ import annotations

import asyncio
import os
import re
import subprocess
from typing import TYPE_CHECKING
from urllib.parse import quote

import httpx

from ..schemas.input import DiffRefs, GitLabMRData
from ..schemas.output import GitLabPosition, GitLabPostResult

if TYPE_CHECKING:
    # Pinned dependency — see pyproject.toml. Only these two Pydantic models
    # are ever imported from pr-af; orchestrator.py/app.py/github/client.py
    # are never touched.
    from pr_af.schemas.output import GitHubComment, GitHubReview

_EVENT_LABELS = {
    "REQUEST_CHANGES": "\U0001f534 Request changes",
    "COMMENT": "\U0001f535 Comment",
    "APPROVE": "\U0001f7e2 Approve",
}


class GitLabClient:
    def __init__(
        self,
        token: str | None = None,
        api_url: str | None = None,
        token_header: str = "PRIVATE-TOKEN",
    ):
        self.token = token or os.getenv("PR_AF_GITLAB_TOKEN") or os.getenv("CI_JOB_TOKEN", "")
        self.api_url = (api_url or os.getenv("CI_API_V4_URL") or "https://gitlab.com/api/v4").rstrip("/")
        self.token_header = token_header

    @staticmethod
    def parse_mr_url(url: str) -> tuple[str, int]:
        """Extract the project path and MR iid from a GitLab MR URL.

        Unlike GitHub's fixed owner/repo, GitLab project paths nest
        arbitrarily (group/subgroup/.../project), so the path segment is
        captured greedily up to ``/-/merge_requests/<iid>``.
        """
        match = re.match(r"https?://[^/]+/(.+)/-/merge_requests/(\d+)", url)
        if not match:
            raise ValueError(f"Invalid GitLab MR URL: {url}")
        return match.group(1), int(match.group(2))

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.token:
            headers[self.token_header] = self.token
        return headers

    @staticmethod
    def _encode_project_id(project_id: str | int) -> str:
        """GitLab's ``:id`` path segment accepts either the numeric project
        id or a URL-encoded namespaced path (``group%2Fsubgroup%2Fproject``).
        Encode paths; pass numeric ids through untouched."""
        text = str(project_id)
        return text if text.isdigit() else quote(text, safe="")

    async def fetch_mr(self, project_id: str | int, mr_iid: int) -> GitLabMRData:
        """Fetch MR metadata and ``diff_refs`` from the GitLab API.

        Retries on transient transport errors and on 5xx/429 responses,
        since a single flaky GitLab call must not sink a whole review.
        4xx (other than 403/429) fail fast. Mirrors
        ``GitHubClient.fetch_pr``'s retry shape exactly.
        """
        last_exc: Exception | None = None
        for attempt in range(4):
            try:
                return await self._fetch_mr_once(project_id, mr_iid)
            except httpx.HTTPStatusError as exc:
                status = exc.response.status_code
                if 400 <= status < 500 and status not in (403, 429):
                    raise
                last_exc = exc
            except (httpx.TransportError, httpx.HTTPError) as exc:
                last_exc = exc
            await asyncio.sleep(2.0 * (attempt + 1))
        assert last_exc is not None
        raise last_exc

    async def _fetch_mr_once(self, project_id: str | int, mr_iid: int) -> GitLabMRData:
        encoded_id = self._encode_project_id(project_id)
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.get(
                f"{self.api_url}/projects/{encoded_id}/merge_requests/{mr_iid}",
                headers=self._headers(),
            )
            resp.raise_for_status()
            data = resp.json()

        diff_refs_raw = data.get("diff_refs") or {}
        diff_refs = (
            DiffRefs(
                base_sha=diff_refs_raw.get("base_sha", ""),
                start_sha=diff_refs_raw.get("start_sha", ""),
                head_sha=diff_refs_raw.get("head_sha", ""),
            )
            if diff_refs_raw
            else None
        )

        return GitLabMRData(
            project_id=data.get("project_id", 0),
            mr_iid=data.get("iid", mr_iid),
            title=data.get("title", ""),
            description=data.get("description") or "",
            labels=data.get("labels", []) or [],
            author=(data.get("author") or {}).get("username", ""),
            source_branch=data.get("source_branch", ""),
            target_branch=data.get("target_branch", ""),
            diff_refs=diff_refs,
        )

    def _build_position(
        self,
        comment: GitHubComment,
        diff_refs: DiffRefs,
        renamed_paths: dict[str, str] | None = None,
    ) -> GitLabPosition | None:
        """Translate one ``GitHubComment{path, line, side, body}`` into a
        GitLab ``position`` object. Returns ``None`` when the comment can't
        be anchored (caller falls back to a plain note) — see PLAN.md §5.1.

        ``renamed_paths`` maps a finding's (new) ``path`` to its previous
        path, sourced from ``ReviewResult.metadata``'s changed-files list
        (``ChangedFile.previous_path``) — ``GitHubComment`` itself carries
        no rename information.
        """
        if not comment.path or comment.line <= 0:
            return None

        renamed_paths = renamed_paths or {}
        new_path = comment.path
        old_path = renamed_paths.get(new_path, new_path)

        if comment.side == "LEFT":
            old_line, new_line = comment.line, None
        else:
            old_line, new_line = None, comment.line

        return GitLabPosition(
            base_sha=diff_refs.base_sha,
            start_sha=diff_refs.start_sha,
            head_sha=diff_refs.head_sha,
            old_path=old_path,
            new_path=new_path,
            old_line=old_line,
            new_line=new_line,
        )

    async def post_review(
        self,
        project_id: str | int,
        mr_iid: int,
        review: GitHubReview,
        diff_refs: DiffRefs | None = None,
        renamed_paths: dict[str, str] | None = None,
    ) -> GitLabPostResult:
        """Post a pr-af review to a GitLab MR.

        Mirrors ``GitHubClient.post_review``'s role, translated to GitLab's
        primitives: one summary note (the review ``body``, with ``event``
        folded into a readable header — GitLab has no native "review
        event"/approval-bundling concept, see PLAN.md §5.1) plus one
        line-anchored discussion per comment when ``diff_refs`` is
        available, falling back to a plain note for anything that can't be
        cleanly anchored. Never silently drops a finding.
        """
        encoded_id = self._encode_project_id(project_id)
        result = GitLabPostResult()

        event_label = _EVENT_LABELS.get(review.event, review.event)
        summary_body = f"**PR-AF Review — {event_label}**\n\n{review.body}"

        print(
            f"[PR-AF-GITLAB] Posting review to project {project_id} MR !{mr_iid}: "
            f"event={review.event}, {len(review.comments)} comments, "
            f"anchored={'yes' if diff_refs else 'no (summary note only)'}",
            flush=True,
        )

        async with httpx.AsyncClient(timeout=60.0) as client:
            note_resp = await client.post(
                f"{self.api_url}/projects/{encoded_id}/merge_requests/{mr_iid}/notes",
                headers=self._headers(),
                json={"body": summary_body},
            )
            if note_resp.status_code >= 400:
                print(f"[PR-AF-GITLAB] GitLab API error {note_resp.status_code}: {note_resp.text}", flush=True)
            note_resp.raise_for_status()
            result.summary_note_id = note_resp.json().get("id")

            if not diff_refs:
                result.skipped_count = len(review.comments)
                if result.skipped_count:
                    print(
                        f"[PR-AF-GITLAB] No diff_refs supplied — posted summary note only, "
                        f"skipped {result.skipped_count} inline comment(s)",
                        flush=True,
                    )
                return result

            for comment in review.comments:
                position = self._build_position(comment, diff_refs, renamed_paths)
                if position is None:
                    await self._record_fallback(client, encoded_id, mr_iid, comment, result)
                    continue

                disc_resp = await client.post(
                    f"{self.api_url}/projects/{encoded_id}/merge_requests/{mr_iid}/discussions",
                    headers=self._headers(),
                    json={"body": comment.body, "position": position.model_dump(exclude_none=True)},
                )
                if disc_resp.status_code >= 400:
                    print(
                        f"[PR-AF-GITLAB] Discussion rejected for {comment.path}:{comment.line} "
                        f"({disc_resp.status_code}): {disc_resp.text} — falling back to a plain note",
                        flush=True,
                    )
                    await self._record_fallback(client, encoded_id, mr_iid, comment, result)
                    continue

                result.discussion_ids.append(str(disc_resp.json().get("id", "")))

        print(
            f"[PR-AF-GITLAB] Review posted: summary_note={result.summary_note_id}, "
            f"{len(result.discussion_ids)} discussion(s), {len(result.fallback_note_ids)} fallback note(s), "
            f"{result.skipped_count} skipped",
            flush=True,
        )
        return result

    async def _record_fallback(
        self,
        client: httpx.AsyncClient,
        encoded_project_id: str,
        mr_iid: int,
        comment: GitHubComment,
        result: GitLabPostResult,
    ) -> None:
        note_id = await self._post_fallback_note(client, encoded_project_id, mr_iid, comment)
        if note_id is not None:
            result.fallback_note_ids.append(note_id)
        else:
            result.skipped_count += 1

    async def _post_fallback_note(
        self,
        client: httpx.AsyncClient,
        encoded_project_id: str,
        mr_iid: int,
        comment: GitHubComment,
    ) -> int | None:
        """Post a plain MR-level note for a finding that couldn't be
        anchored to a diff line. Never raises — a failed fallback must not
        sink the rest of the review."""
        location = f"{comment.path}:{comment.line}" if comment.path else "(unanchored)"
        body = f"**{location}**\n\n{comment.body}"
        try:
            resp = await client.post(
                f"{self.api_url}/projects/{encoded_project_id}/merge_requests/{mr_iid}/notes",
                headers=self._headers(),
                json={"body": body},
            )
            resp.raise_for_status()
            return resp.json().get("id")
        except httpx.HTTPError as exc:
            print(f"[PR-AF-GITLAB] Fallback note failed for {location}: {exc}", flush=True)
            return None

    async def clone_repo(self, project_path: str, target_dir: str, shallow: bool = True) -> str:
        """Clone a GitLab project to a local path. Returns the path.

        Mirrors ``GitHubClient.clone_repo``'s role in the optional
        webhook-triggered flow (PLAN.md §12.6) — NOT used on the CI
        critical path, since a CI job's own checkout already is the repo.
        """
        token = self.token
        if not token:
            raise ValueError("A GitLab token is required for clone_repo")

        host = os.getenv("CI_SERVER_HOST", "gitlab.com")
        repo_url = f"https://oauth2:{token}@{host}/{project_path}.git"
        command = ["git", "clone"]
        if shallow:
            command.extend(["--depth", "1"])
        command.extend([repo_url, target_dir])

        subprocess.run(command, check=True, capture_output=True, text=True)
        return target_dir
