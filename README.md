# pr-af-gitlab

A GitLab adapter for [`Agent-Field/pr-af`](https://github.com/Agent-Field/pr-af) — an AI-native, multi-agent pull-request reviewer. `pr-af` itself is GitHub-only; this repo adds a thin GitLab layer around its unmodified review pipeline so it can review GitLab merge requests instead, running on Anthropic's API via the `claude-code` provider.

`pr-af`'s core pipeline is reused **completely unchanged** — this repo never forks or modifies it. See [`PLAN.md`](PLAN.md) for the full design rationale, the investigation this was built on, and every open decision and known risk.

## How it works

1. `pr-af` already supports driving a review from a local checkout (`repo_path` + `base_ref`/`head_ref`) with no GitHub coupling at all. A GitLab CI job's own checkout is exactly that.
2. This repo's `docker-compose.yml` brings up the AgentField control plane + `pr-af` agent, built from this repo's own [`Dockerfile`](Dockerfile) — which fetches `pr-af`'s source from its pinned commit, adds the one dependency (`agentfield[harness-claude]`) `claude-code` needs that upstream's own image doesn't install, and swaps in [`docker/entrypoint_wrapper.py`](docker/entrypoint_wrapper.py) to route `pr-af`'s `.ai()` calls to Anthropic directly instead of its hardcoded OpenRouter endpoint. None of `pr-af`'s own source is patched or forked — see `.claude/steps/002-dockerfile-claude-code-and-anthropic-routing/`.
3. `scripts/ci_runner.py` fires a review against the control plane's HTTP API, polls until it completes, fetches the full result, and hands it to `GitLabClient`.
4. `GitLabClient` (mirrors `pr-af`'s `GitHubClient` method-for-method — see `PLAN.md` §5) translates the result into GitLab MR discussions, using the `position` object for line-anchored comments.

**This repo's own `.gitlab-ci.yml` only lints/tests this repo's code.** It does not review its own merge requests — the actual review-trigger job is [`templates/pr-af-review.gitlab-ci.yml`](templates/pr-af-review.gitlab-ci.yml), meant to run in *other* projects (see below).

## Setup

In the project you want `pr-af` to review:

1. Enable **"Pipelines for merge requests"** (Settings → CI/CD → General pipelines, or ensure your `.gitlab-ci.yml`'s `workflow:` rules permit `merge_request_event` pipelines).
2. Add these CI/CD variables (Settings → CI/CD → Variables), all **masked + protected**:
   - `ANTHROPIC_API_KEY` — required. Powers `.harness()` calls (planner/reviewer/cross_ref/adversary) directly, and — via `docker/entrypoint_wrapper.py`'s runtime override — the `.ai()` comment-polish and merge-blocking-gate passes too, so no code/finding text reaches OpenRouter as long as this is set.
   - `PR_AF_GITLAB_TOKEN` — a **Project or Group Access Token** (scope: `api`, no wider) for the bot identity that posts discussions. **Required, not just recommended** — confirmed against GitLab's own docs that `CI_JOB_TOKEN` only ever gets read access to the Notes API, never write, so it cannot post discussions under any GitLab version or config.
   - `OPENROUTER_API_KEY` — only needed as a fallback for deployments that haven't set `ANTHROPIC_API_KEY` (pr-af's original default behavior). Not required once `ANTHROPIC_API_KEY` is set.
3. Add the review-trigger job — see the two options below.
4. Apply the **`pr-af`** label to a merge request to trigger a review.

### Recommended: `include:`

```yaml
include:
  - project: '<group>/pr-af-gitlab'
    ref: 'v1.0.0' # pin to a released tag; bump deliberately
    file: '/templates/pr-af-review.gitlab-ci.yml'
```

Gives every adopter a single source of truth, updatable by bumping one `ref:`.

### Fallback: copy-paste

If your runner can't reach wherever this repo is hosted (air-gapped/network-isolated GitLab instances), copy [`templates/pr-af-review.gitlab-ci.yml`](templates/pr-af-review.gitlab-ci.yml)'s job directly into your own `.gitlab-ci.yml`. Functionally identical, just duplicated instead of included — you'll need to manually re-copy it when this repo updates.

## Configuration

| Variable | Purpose |
|---|---|
| `ANTHROPIC_API_KEY` | Anthropic API key — required for `.harness()` calls, and (via the entrypoint wrapper) the `.ai()` polish/merge-gate passes. |
| `PR_AF_GITLAB_TOKEN` | Token used to post MR discussions. `CI_JOB_TOKEN` is **not** a working fallback for posting (confirmed read-only on the Notes API) — only useful as a `fetch_mr` read fallback. |
| `PR_AF_GITLAB_TOKEN_HEADER` | `PRIVATE-TOKEN` (default, for a PAT/access token). `JOB-TOKEN` exists for completeness, not as a posting alternative. |
| `PR_AF_PROVIDER` / `PR_AF_MODEL` | Harness provider/model (`.harness()`, native `claude_agent_sdk` — bare model id) — default `claude-code` / `claude-sonnet-4-5`. |
| `PR_AF_AI_MODEL` | `.ai()` model (litellm-routed — needs the `anthropic/` prefix to route to Anthropic directly) — default `anthropic/claude-sonnet-4-5`. |
| `PR_AF_DEPTH` | `auto` (default, size-based escalation) \| `quick` \| `standard` \| `deep`. |
| `PR_AF_GITLAB_LINE_ANCHORED` | `1` (default, Phase B: line-anchored discussions) or `0` (Phase A: summary note only). |
| `PR_AF_GITLAB_MIN_SEVERITY` | Minimum finding severity to post — default `nitpick`. |
| `PR_AF_GITLAB_MAX_DISCUSSIONS` | Cap on inline discussions per review — default `25`. |
| `PR_AF_GITLAB_REPO` / `PR_AF_GITLAB_REF` | Where the template clones this repo from, and at which pinned ref — set these if you're not using GitLab.com or want a different pin than the template's default. |

## Phases

- **Phase A — summary note.** `PR_AF_GITLAB_LINE_ANCHORED=0`: one MR-level note summarizing findings. Fastest path to an end-to-end working pipeline.
- **Phase B — line-anchored discussions** (default). One discussion per in-diff finding, anchored via the GitLab `position` object; anything that can't be cleanly anchored falls back to a plain note rather than being dropped.
- **Phase C — hardening.** Pinning the control-plane image to a digest, confirming the pinned `pr-af` commit still builds cleanly after any deliberate ref bump. Tracked in `PLAN.md` §9.

## Known limitations (see `PLAN.md` and `.claude/steps/` for full detail)

- `pr-af`'s `ReviewResult` doesn't expose rename information for changed files — a finding on a renamed file will anchor `old_path == new_path` until a future `pr-af` release surfaces `ChangedFile.previous_path` through the API.
- A comment on a genuinely *unchanged* diff line should set both `old_line`/`new_line` per GitLab's Discussions API — `GitLabClient._build_position` always sets exactly one (mirroring GitHub's LEFT/RIGHT model). `pr-af` findings essentially never target unchanged lines, so this is a documented gap, not a redesign.
- Several CI predefined variables and the exact GitLab Discussions/`position` API contract were verified against GitLab 17.7 CE docs specifically — re-verify if you're on a materially different version.

## Development

```bash
pip install -e ".[dev]"
pytest tests/ -v
ruff check src/ scripts/ tests/
```

Tests run without a live GitLab instance or the pinned `pr-af` dependency installed (`httpx` is monkeypatched with a small recording fake; `pr-af`'s `GitHubComment`/`GitHubReview` are stood in for locally — see the test files' docstrings). A manual end-to-end test plan against a live low-stakes GitLab project is in `PLAN.md` §10.
