# Execution Summary — claude-code provider fix + Anthropic-direct `.ai()` routing

## What was built

| Path | What it is |
|---|---|
| `Dockerfile` (new, repo root) | Mirrors `pr-af`'s own two-stage `Dockerfile`, with three deltas: fetches `pr-af`'s source from its pinned commit's GitHub tarball instead of a local build context; installs `agentfield[harness-claude]>=0.1.84` instead of plain `agentfield`; copies in and runs `docker/entrypoint_wrapper.py` instead of `python -m pr_af.app`. |
| `docker/entrypoint_wrapper.py` (new) | Reassigns `pr_af.app.app.ai_config` after import (before the server starts) to route `.ai()` through Anthropic directly when `ANTHROPIC_API_KEY` is set — no source patch to `pr-af`. |
| `docker-compose.yml` | `pr-af` service's `build.context` changed from the remote git URL to `.`; added `PR_AF_AI_MODEL` (distinct from `PR_AF_MODEL` — see below); updated the surrounding comment. |
| `src/pr_af_gitlab/config.py` | `CI_JOB_TOKEN` comment updated from "verify" to "confirmed cannot post." |
| `src/pr_af_gitlab/gitlab/client.py` | Added a comment documenting the unchanged-context-line position-mapping gap. |
| `README.md` | Reflects the new `Dockerfile`/`docker/` files, corrected `CI_JOB_TOKEN` guidance, new `PR_AF_AI_MODEL` config row, updated known-limitations list. |
| Root `PLAN.md` | Amendment notes added near §3, §7.1, §7.2, §12.4, §12.7 — pointing at this step, original text left intact. |
| `.claude/README.md` | Index row appended for this step. |

## Why this step happened at all

Discussing PLAN.md's open questions with Jeff led to picking concrete resolutions for the OpenRouter question (§7.2) and the token model (§7.1). Actually implementing the OpenRouter fix required understanding `agentfield`'s real internals, not the assumptions PLAN.md §7.2 had made — that investigation (downloading the real `agentfield` 0.1.111 wheel and reading its source directly, plus fetching GitLab's own current docs) surfaced a second, more urgent problem that PLAN.md hadn't caught at all: **`PR_AF_PROVIDER=claude-code`, already the default in `docker-compose.yml` since step 001, didn't actually work** — the container it built lacked `claude_agent_sdk` entirely. That's a correctness bug in already-committed code, not a hypothetical, which is why this became its own planned step rather than a quick fix.

## Verification actually performed

All five items from this step's `PLAN.md` were run for real (Docker is available locally), not just described:

1. `docker build -t pr-af-gitlab-image:test .` — **succeeded** (~76s for the dependency install layer, confirmed `claude-agent-sdk-0.2.124` and `agentfield-0.1.111` in the installed package list).
2. `docker run --rm --entrypoint python pr-af-gitlab-image:test -c "import claude_agent_sdk; ..."` — **printed `claude_agent_sdk OK`**. Confirms the actual fix for the provider bug.
3. `docker run ... ` running the override logic inline — **printed `api_base: None`, `model: anthropic/claude-sonnet-4-5`**. Confirms the reassignment logic behaves as designed inside the real built image.
4. `docker compose config` — **succeeded**, confirmed `build.context` resolves to the local repo path and both `PR_AF_MODEL=claude-sonnet-4-5` / `PR_AF_AI_MODEL=anthropic/claude-sonnet-4-5` show as distinct, correct values in the merged config.
5. `pytest tests/ -v` — **23/23 passed**. `ruff check src/ scripts/ tests/` — **clean**. Confirms the comment-only `config.py`/`client.py` edits didn't break anything.

The test image was removed after verification (`docker rmi pr-af-gitlab-image:test`) — it was only needed to prove the build/runtime behavior, not to keep around.

**Not covered, same as step 001:** an actual live review run against a real GitLab MR with real API keys — still requires PLAN.md §10's manual end-to-end test plan and a real instance/keys neither of us has run yet.

## Design choice worth calling out: runtime override, not a source patch

The approved plan initially assumed a unified-diff `.patch` file against `pr-af`'s `app.py` would be needed (per Jeff's "carry a small local patch" decision). During planning, reading `agentfield`'s actual `agent.py`/`agent_ai.py` source showed `Agent.ai_config` is a plain mutable attribute read fresh on every `.ai()` call — which meant the fix could be a runtime reassignment (`docker/entrypoint_wrapper.py`) instead of a source patch. This is strictly less invasive (zero bytes of `pr-af`'s own file touched, no `patch` tool needed, no risk of a unified diff failing to apply after a future upstream ref bump) while achieving exactly what Jeff asked for. Flagging this because the mechanism that shipped is different from what was literally decided in the prior discussion, even though the *outcome* (`.ai()` routes to Anthropic when `ANTHROPIC_API_KEY` is set) is exactly what was asked for.

## Still open

- PLAN.md §12.6 (MR-comment webhook) — separately deferred pending its own hosting-focused plan, untouched by this step.
- A live end-to-end run against a real GitLab MR (PLAN.md §10) — still not performed.
- The `Dockerfile`'s tarball-fetch approach depends on GitHub's `archive/<sha>.tar.gz` endpoint remaining available — a reasonable assumption (long-standing, official GitHub feature) but not independently verified beyond this step's successful build.
