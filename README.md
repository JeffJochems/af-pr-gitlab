# pr-af-gitlab

A GitLab adapter for [`Agent-Field/pr-af`](https://github.com/Agent-Field/pr-af) — an AI-native, multi-agent pull-request reviewer. `pr-af` itself is GitHub-only; this repo adds a thin GitLab layer around its unmodified review pipeline so it can review GitLab merge requests instead, running on Anthropic's API via the `claude-code` provider.

`pr-af`'s core pipeline is reused **completely unchanged** — this repo never forks or modifies it. See [`PLAN.md`](PLAN.md) for the full design rationale, the investigation this was built on, and every open decision and known risk.

## How it works

1. `pr-af` already supports driving a review from a local checkout (`repo_path` + `base_ref`/`head_ref`) with no GitHub coupling at all. A GitLab CI job's own checkout is exactly that.
2. This repo's `docker-compose.yml` brings up the AgentField control plane + `pr-af` agent (built straight from `pr-af`'s own pinned Dockerfile — nothing local to build or maintain).
3. `scripts/ci_runner.py` fires a review against the control plane's HTTP API, polls until it completes, fetches the full result, and hands it to `GitLabClient`.
4. `GitLabClient` (mirrors `pr-af`'s `GitHubClient` method-for-method — see `PLAN.md` §5) translates the result into GitLab MR discussions, using the `position` object for line-anchored comments.

**This repo's own `.gitlab-ci.yml` only lints/tests this repo's code.** It does not review its own merge requests — the actual review-trigger job is [`templates/pr-af-review.gitlab-ci.yml`](templates/pr-af-review.gitlab-ci.yml), meant to run in *other* projects (see below).

## Setup

In the project you want `pr-af` to review:

1. Enable **"Pipelines for merge requests"** (Settings → CI/CD → General pipelines, or ensure your `.gitlab-ci.yml`'s `workflow:` rules permit `merge_request_event` pipelines).
2. Add these CI/CD variables (Settings → CI/CD → Variables), all **masked + protected**:
   - `ANTHROPIC_API_KEY` — required, powers the review agents directly via Anthropic.
   - `PR_AF_GITLAB_TOKEN` — a **Project or Group Access Token** (scope: `api`, no wider) for the bot identity that posts discussions. Recommended over `CI_JOB_TOKEN`, whose scope for posting discussions is version- and instance-config-dependent — see `PLAN.md` §7.1. If you've confirmed `CI_JOB_TOKEN` works on your instance, you can skip this; the client falls back to it automatically.
   - `OPENROUTER_API_KEY` — **only until `PLAN.md` §7.2/§12.7 is resolved.** `pr-af`'s own lightweight classification/polish passes are currently hardcoded to OpenRouter regardless of the main provider setting, and at least two of them (comment polish, the merge-blocking gate) do transmit finding text — read §7.2 before deciding whether this is acceptable for your data-governance posture.
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
| `ANTHROPIC_API_KEY` | Anthropic API key — required for `.harness()` calls (planner/reviewer/cross_ref/adversary). |
| `PR_AF_GITLAB_TOKEN` | Token used to post MR discussions. Falls back to `CI_JOB_TOKEN` if unset. |
| `PR_AF_GITLAB_TOKEN_HEADER` | `PRIVATE-TOKEN` (default, for a PAT/access token) or `JOB-TOKEN` (only after confirming `CI_JOB_TOKEN` scope). |
| `PR_AF_PROVIDER` / `PR_AF_MODEL` | Harness provider/model — default `claude-code` / `claude-sonnet-4-5`. |
| `PR_AF_DEPTH` | `auto` (default, size-based escalation) \| `quick` \| `standard` \| `deep`. |
| `PR_AF_GITLAB_LINE_ANCHORED` | `1` (default, Phase B: line-anchored discussions) or `0` (Phase A: summary note only). |
| `PR_AF_GITLAB_MIN_SEVERITY` | Minimum finding severity to post — default `nitpick`. |
| `PR_AF_GITLAB_MAX_DISCUSSIONS` | Cap on inline discussions per review — default `25`. |
| `PR_AF_GITLAB_REPO` / `PR_AF_GITLAB_REF` | Where the template clones this repo from, and at which pinned ref — set these if you're not using GitLab.com or want a different pin than the template's default. |

## Phases

- **Phase A — summary note.** `PR_AF_GITLAB_LINE_ANCHORED=0`: one MR-level note summarizing findings. Fastest path to an end-to-end working pipeline.
- **Phase B — line-anchored discussions** (default). One discussion per in-diff finding, anchored via the GitLab `position` object; anything that can't be cleanly anchored falls back to a plain note rather than being dropped.
- **Phase C — hardening.** Resolving `PLAN.md` §7.2 (the OpenRouter question), confirming the token model against your instance, pinning the control-plane image to a digest. Tracked in `PLAN.md` §9.

## Known limitations (see `PLAN.md` for full detail)

- `pr-af`'s `ReviewResult` doesn't expose rename information for changed files — a finding on a renamed file will anchor `old_path == new_path` until a future `pr-af` release surfaces `ChangedFile.previous_path` through the API.
- The GitLab Discussions/`position` API contract, `CI_JOB_TOKEN` scope, and several CI predefined variables are version-dependent — verify against your instance (`PLAN.md` §13) before relying on this in production.
- `pr-af`'s own comment-polish and merge-blocking-gate passes are hardcoded to OpenRouter (`PLAN.md` §7.2) — read this before treating the pipeline as Anthropic-only.

## Development

```bash
pip install -e ".[dev]"
pytest tests/ -v
ruff check src/ scripts/ tests/
```

Tests run without a live GitLab instance or the pinned `pr-af` dependency installed (`httpx` is monkeypatched with a small recording fake; `pr-af`'s `GitHubComment`/`GitHubReview` are stood in for locally — see the test files' docstrings). A manual end-to-end test plan against a live low-stakes GitLab project is in `PLAN.md` §10.
