# Execution Summary — pr-af-gitlab implementation

This records what Claude Code actually did in the implementation pass that followed `PLAN.md`'s approval, including the things the plan didn't anticipate. `PLAN.md` (copied alongside this file) is the design document; this is the as-built record.

## What was built

| Path | What it is |
|---|---|
| `src/pr_af_gitlab/gitlab/client.py` | `GitLabClient` — mirrors `pr_af.github.client.GitHubClient` method-for-method: `parse_mr_url`, `fetch_mr`, `post_review`, `clone_repo`. Implements the full `position`-object mapping from PLAN.md §5.1 (RIGHT/LEFT → new_line/old_line, rename handling via a caller-supplied map, fallback-to-plain-note when a discussion is rejected or a finding can't be anchored — no silent drops). |
| `src/pr_af_gitlab/schemas/input.py`, `output.py` | `GitLabMRData`/`DiffRefs` (data fetched from GitLab) and `GitLabPosition`/`GitLabDiscussion`/`GitLabNote`/`GitLabPostResult` (data sent to GitLab). |
| `src/pr_af_gitlab/config.py` | `GitLabIntegrationConfig` — env-driven, mirrors `pr_af.config`'s `Field(default_factory=lambda: os.getenv(...))` idiom. |
| `docker-compose.yml` | Upstream's two services reused near-verbatim; `pr-af`'s build context points at the pinned upstream commit via a remote git build context (BuildKit) rather than a local copy of its source. |
| `scripts/ci_runner.py`, `scripts/wait_for_health.sh` | Fires the async review, polls the control plane, fetches the full `ReviewResult`, and hands it to `GitLabClient.post_review`. |
| `templates/pr-af-review.gitlab-ci.yml` | The includable review-trigger job for *adopter* projects (not run against this repo itself). |
| `.gitlab-ci.yml` (repo root) | This repo's own lint/test/build pipeline only — mirrors upstream's `ci.yml` purpose exactly. |
| `tests/test_gitlab_client.py`, `test_position_mapping.py`, `test_ci_runner.py` | 23 tests, all passing, no live GitLab/pr-af instance required. |
| `README.md` | Setup instructions, configuration table, phases, known limitations. |

Pinned upstream commit used throughout (pyproject.toml dependency + docker-compose.yml build context): `6b82efc8ade7cd48420ecd6de59eeb1cb80d3b49` (`Agent-Field/pr-af`, no tags existed upstream at investigation time, so HEAD was pinned by SHA).

## Verification actually performed

- `pytest tests/ -v` — **23/23 passed**.
- `ruff check src/ scripts/ tests/` — clean (2 minor auto-fixable nits found and fixed: unsorted import, unnecessary quoted type annotation).
- Both re-run after fixes to confirm nothing regressed.
- Not performed (requires infrastructure this pass didn't have): a live GitLab instance end-to-end run, or installing the real pinned `pr-af` package (the tests deliberately avoid needing it — see the test files' docstrings for why). Both are called out as required next steps in `PLAN.md` §10 and are still outstanding.

## Two real bugs found and fixed during implementation, not caught at planning time

Both are genuine architecture-level mistakes the plan didn't surface — worth flagging explicitly rather than folding in silently:

1. **DinD filesystem isolation.** `pr-af` runs in its own container, brought up via `docker compose` from inside the CI job (typically against a `docker:dind` service). That container does **not** share the job's own filesystem, so passing `repo_path=$CI_PROJECT_DIR` straight through — which is what a first-pass implementation naturally does — silently points at a path that doesn't exist inside the `pr-af` container. Fixed by having the CI template `docker compose cp "$CI_PROJECT_DIR" pr-af:$PR_AF_CONTAINER_REPO_PATH` before invoking `ci_runner.py`, and having `ci_runner.py` read `PR_AF_CONTAINER_REPO_PATH` (a path valid *inside* the `pr-af` container) instead of `CI_PROJECT_DIR` directly.
2. **DinD networking.** From the job container, ports published by containers started via the `docker:dind` service are reachable at the `docker` service alias — not `localhost`. `AGENTFIELD_SERVER` (used by `ci_runner.py` to reach the control plane) and the health-check URL are both set to `http://docker:8080` / `http://docker:8004/health` in the template accordingly, not the `localhost` defaults that would work in a plain local `docker compose up`.

Both are documented inline as comments at the point they're handled (`scripts/ci_runner.py`'s `main()` docstring-comment, `templates/pr-af-review.gitlab-ci.yml`'s `variables:` block) so a future reader hits the explanation exactly where the non-obvious behavior lives.

## One thing PLAN.md asserted that turned out to be wrong

PLAN.md §5.1 says renamed-file positions can be built by "cross-referencing `pr_data.changed_files[].previous_path`" from `ReviewResult.metadata`. While implementing `ci_runner.py`'s `_post_to_gitlab`, this turned out not to exist: `ReviewMetadata` (upstream's own schema) only carries `intake`/`anatomy`/`plan`/`budget` dicts, and neither `IntakeResult` nor `AnatomyResult` carries `ChangedFile.previous_path` anywhere. There is currently **no way** to recover rename information from the API response at all. `GitLabClient._build_position`'s `renamed_paths` parameter still exists and is tested (in case a future `pr-af` release exposes this), but in production today it's always empty, and `ci_runner.py` carries a `KNOWN GAP` comment saying so explicitly rather than silently anchoring renamed files incorrectly.

## Cleanups made after first-draft implementation

- Removed a `[project.scripts]` entry point from `pyproject.toml` that would not actually have worked (`scripts/` isn't a proper importable package, and the CI template invokes `scripts/ci_runner.py` directly anyway — the entry point was dead weight).
- Removed an unused `python-dotenv` dependency (nothing in this repo loads a `.env` file, unlike upstream's `app.py`).
- Reordered `_post_to_gitlab`'s imports so the "not a merge-request pipeline, nothing to post" early-return path doesn't require the pinned `pr-af` git dependency to be importable — both a correctness fix (lets that path be unit-tested without a network install) and a minor quality improvement (defers an expensive import until it's actually needed).

## Still open — unchanged from PLAN.md, not resolved by this pass

Nothing in this implementation pass resolves these; they need a decision from Jeff or access to a live instance, not more code:

- **PLAN.md §7.2 / §12.7** — `pr-af`'s comment-polish and merge-blocking-gate passes are hardcoded to OpenRouter regardless of the main provider setting, and do transmit finding text (not just metadata). `docker-compose.yml` and the CI template both still reference `OPENROUTER_API_KEY` and carry comments pointing back at this open decision — it has not been quietly resolved or worked around.
- **PLAN.md §7.1** — final token model (dedicated Project/Group Access Token vs. `CI_JOB_TOKEN`) — `GitLabIntegrationConfig` supports both, defaulting to the recommended access-token path, but the decision itself is still Jeff's to confirm against the real instance.
- **PLAN.md §13** — GitLab Discussions/`position` API contract, CI predefined variable behavior, and `CI_JOB_TOKEN` scope are all version-dependent and unverified against a live instance.
- A live end-to-end run against a real, low-stakes GitLab project (PLAN.md §10's manual test plan) has not been performed.
