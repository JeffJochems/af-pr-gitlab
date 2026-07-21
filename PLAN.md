# PLAN.md — GitLab Adapter for PR-AF

**Status:** Draft for review. No implementation has started. This document is the sole deliverable of this pass.
**Author:** Claude Code, for Jeff Jochems
**Upstream studied:** [`Agent-Field/pr-af`](https://github.com/Agent-Field/pr-af), cloned read-only at HEAD (2026-07-21) for investigation. No changes were made to it.

---

## 1. Summary

We will build a thin GitLab adapter around `pr-af`'s unmodified review pipeline, shipped as a new repository (`pr-af-gitlab`, name TBD — see §12.1). Mirroring a pattern already confirmed in `pr-af` itself (§2.1: its GitHub Actions review-trigger job is a README snippet adopters add to *their own* repo — `pr-af`'s own `.github/workflows/` only contains a lint/build/test pipeline, never a file that reviews `pr-af`'s own PRs), **this repo's `.gitlab-ci.yml` is limited to linting/testing the adapter's own code.** It does not trigger a review of its own merge requests — that would make no sense, since the job's entire purpose is to review *other* projects' code. The actual review-trigger CI job is shipped as an includable template (`templates/pr-af-review.gitlab-ci.yml`) plus a matching README snippet, for adopters to add to the `.gitlab-ci.yml` of the project being reviewed — see §8.

The adapter does **not** import or fork `pr-af`'s orchestrator, agents, or GitHub client. Instead, it drives `pr-af` purely through its existing HTTP execution API (`POST /api/v1/execute/async/pr-af.review`) using `pr-af`'s already-generic **Mode 3** input (`repo_path` + `base_ref`/`head_ref` from the GitLab CI checkout), and consumes the JSON `ReviewResult` that comes back. A small `GitLabClient` — mirroring `GitHubClient` method-for-method — then translates that JSON into GitLab MR discussions via the `position` object API.

This works because of two things confirmed during investigation: (1) `pr-af`'s core pipeline genuinely has zero GitHub coupling — the diff/finding/scoring machinery is host-agnostic — and (2) `pr-af` already runs as a self-contained Docker Compose stack (AgentField control plane + agent) that any caller drives over HTTP, exactly as its own `scripts/ci_runner.py` does today. Our adapter is architecturally the same kind of caller `ci_runner.py` already is, just one that also posts results to GitLab afterward.

The one genuine wrinkle, surfaced during investigation and **not** assumed away, is that `pr-af`'s lightweight `.ai()` gate calls (used for `polish_comments` and the merge-blocking gate) are hardcoded in `app.py` to OpenRouter, and at least two of those calls do transmit finding bodies containing quoted source code and suggested fixes — not just metadata. This conflicts with the "Anthropic only" data-governance decision as stated, and is flagged prominently in §7 and §13 as a decision Jeff needs to make before Phase A ships, not something this plan silently works around.

---

## 2. Investigation findings

### 2.1 What's confirmed about `pr-af`

- **Core pipeline has no GitHub coupling**, confirmed by direct import inspection: `diff_engine.py`, `evidence.py`, `blast_radius.py`, `merge_gate.py`, `scoring.py` import nothing from `.github`. The class names `GitHubPRData`, `GitHubComment`, `GitHubReview` in `schemas/input.py`/`schemas/output.py` are GitHub-flavored *names* for otherwise generic DTOs (path/line/side/body; owner/repo/number/diff/changed_files) — the fields themselves carry no GitHub-specific behavior.
- **`orchestrator.py` touches GitHub in exactly two places**, but both are **hardcoded instantiations**, not an injected interface:
  - `_run_intake()` (`orchestrator.py:396-397`): `if self.input.pr_url: client = GitHubClient(); self.pr_data = await client.fetch_pr(self.input.pr_url)`.
  - `_generate_output()` (`orchestrator.py:1187-1188`): `if post_to_github and not self.input.dry_run and self.input.pr_url: client = GitHubClient(); await client.post_review(...)`.
  - **Consequence for our design:** there is no dependency-injection seam to swap in a `GitLabClient` without either monkeypatching the `pr_af.github.client` module or forking `orchestrator.py`. Both violate the "don't modify/fork core" non-goal. This is *why* the integration strategy in §3 drives everything through `repo_path` + `dry_run=True` and does all GitLab posting **outside** the `pr-af` process, rather than trying to make `GitLabClient` a drop-in replacement inside it.
- **Three input modes exist exactly as described**, in `schemas/input.py::ReviewInput` and consumed in `orchestrator.py::_run_intake`: `pr_url` (GitHub), `diff_text` (raw diff), `repo_path`+`base_ref`+`head_ref` (local checkout — computes its own diff via `_compute_repo_diff`, not shown above but confirmed called at `orchestrator.py:410`). Mode 3 has zero GitHub coupling and is exactly what a GitLab CI job can drive without writing any fetch code.
- **The GitHub-specific surface is `src/pr_af/github/client.py`** (302 lines) — `GitHubClient` with:
  - `parse_pr_url(url) -> (owner, repo, number)` — static, regex-based.
  - `fetch_pr(pr_url) -> GitHubPRData` — retries on 5xx/429/transport errors, paginates files and commits, fetches the unified diff via the `.v3.diff` media type.
  - `post_review(owner, repo, pr_number, review: GitHubReview, commit_sha) -> dict` — POSTs `{body, event, commit_id, comments: [{path, line, side, body}]}` to `/repos/{owner}/{repo}/pulls/{pr_number}/reviews`.
  - `clone_repo(owner, repo, target_dir, shallow) -> str` — tokenized HTTPS clone (`https://x-access-token:{token}@github.com/...`).
  - Also handles GitHub App JWT/installation-token auth as an alternative to a plain PAT (`_generate_app_jwt`, `_get_installation_token`) — not something GitLab has an equivalent of; out of scope for us.
- **`app.py`'s GitHub surface** is the `@app.reasoner() async def review(...)` entrypoint (which builds `ReviewInput` and runs `ReviewOrchestrator`) plus a GitHub-only `webhook_github` handler (issue-comment `@pr-af` mention → fires an async review via the control-plane HTTP API). There is **no GitHub Actions workflow file shipped in the repo** — `.github/workflows/` contains only `ci.yml` (lint/test/build, not a review trigger). The label-triggered review workflow the README describes (`.github/workflows/pr-af-review.yml`, triggered on `pull_request: types: [labeled]` with label `pr-af`) is **documentation the README tells adopters to add to their own repo**, not a file in this repo. This corrects the brief's assumption in its §3 — there is no existing workflow file to "re-implement," only a README snippet, reproduced in full in §2.3 below since it's the actual reference for our `.gitlab-ci.yml`.
- **`config.py`** confirms the fixed-decision assumptions partially:
  - `AIIntegrationConfig.provider` ← `PR_AF_PROVIDER` env (default `"opencode"`); `harness_model` ← `PR_AF_MODEL`; `provider_env()` whitelists and forwards `ANTHROPIC_API_KEY` (among others) into the harness subprocess environment. This path is genuinely provider-agnostic and Anthropic-direct via `claude-code` is a supported, code-confirmed path for **`.harness()` calls** (planner, reviewer, cross_ref, adversary — per `ModelConfig`, the "premium"-tier, most-of-the-cost phases).
  - **However**, `app.py:45-49` constructs the harness's `.ai()` client as `AIConfig(model=_ai_config.ai_model, api_key=os.getenv("OPENROUTER_API_KEY", ""), api_base="https://openrouter.ai/api/v1")` — **hardcoded, not env-branch-selectable**, regardless of `PR_AF_PROVIDER`. This is a real discrepancy from the brief's assumption that "OpenRouter is only the README's default example, not a hard dependency" — it is a hardcoded dependency for the `.ai()` code path specifically. See §7.2 for the full analysis; this is the single most important open risk in this plan.
- **The `SUPPORTED_PROVIDERS = {"claude-code", "codex", "gemini", "opencode"}` constant the brief cites was not found anywhere in `pr-af`'s own source.** It must live in the external `agentfield` PyPI package (`agentfield>=0.1.84`, a pinned dependency, not vendored into this repo), which this investigation pass had no access to. **Flagged for verification** against the actually-installed `agentfield` version — see §13.
- **`docker-compose.yml`** confirms the deployment model: `agentfield/control-plane:latest` + a `pr-af` service built from the local `Dockerfile`, talking to each other over the compose network, with `PR_AF_PROVIDER`/`PR_AF_MODEL`/`OPENROUTER_API_KEY`/`GH_TOKEN` passed straight through as environment variables. This is the exact stack our GitLab CI job needs to bring up.
- **`scripts/ci_runner.py`** is a ~80-line script: POST `{"input": {"pr_url", "depth": "standard", "dry_run": False}}` to `/api/v1/execute/async/pr-af.review`, then poll `/api/ui/v1/executions/{id}/details` every 30s for `status` (`succeeded`/`failed`/`cancelled`), for up to ~30-60 minutes. It never fetches the actual `ReviewResult` payload — it only cares about pass/fail for CI purposes, because upstream's own posting happens *inside* the `pr-af` container via `GitHubClient.post_review`, not by the CI runner. The README separately documents `GET /api/v1/executions/<execution_id>` as the endpoint that returns the full result — this is the one our GitLab CI runner needs, since we must fetch `ReviewResult.review` (the `GitHubReview`-shaped JSON: `body`, `event`, `comments: [{path, line, side, body}]`) ourselves and translate it to GitLab discussions.
- **Tests** (`tests/*.py`) are flat pytest files at repo root (no subfolder nesting), named `test_<subject>.py`, favoring real temporary git repos over mocks where feasible (`test_resolve_repo.py` builds actual local git remotes rather than mocking subprocess calls), with docstrings explaining the regression each test guards against. This is the convention to mirror.
- **There is also a Go port** under `go/`, registering as a separate node `pr-af-go` (port 8007) alongside the Python node `pr-af` (port 8004) — "Python is the default," Go is opt-in. Not relevant to this adapter; noted for completeness since it appears in the repo tree and could otherwise cause confusion when navigating the source.
- **Benchmark/README calibration**: the README's headline claim is "**0.706 golden recall — #1 open source across 42 compared tools**" (a recall claim), not a "zero false positives" claim as such — the closest matching text is a comparison table cell reading "**Extremely low** (Evidence Grounding)" for false positives, framed relative to competitors, not as an absolute "zero" guarantee. Calibrate expectations accordingly: the tool is recall-optimized and evidence-grounded to suppress false positives, not proven false-positive-free.

### 2.2 File-by-file mapping table

| Upstream file (`pr-af`) | Adapter file (`pr-af-gitlab`) | Notes |
|---|---|---|
| `src/pr_af/github/client.py` | `src/pr_af_gitlab/gitlab/client.py` | New. `GitLabClient` mirrors `GitHubClient` method-for-method: `parse_mr_url`, `fetch_mr` (optional, §12.3), `post_review`, `clone_repo`. Same `httpx.AsyncClient`, same retry/print-logging conventions. |
| `src/pr_af/github/__init__.py` | `src/pr_af_gitlab/gitlab/__init__.py` | New, trivial re-export. |
| `src/pr_af/schemas/input.py` (`GitHubPRData`) | `src/pr_af_gitlab/schemas/input.py` (`GitLabMRData`) | New, only if `fetch_mr` is adopted (§12.3). `ChangedFile` is already host-agnostic — **reused unchanged** (imported, not redefined), consistent with "mirror, don't fork." |
| `src/pr_af/schemas/output.py` (`GitHubComment`, `GitHubReview`) | `src/pr_af_gitlab/schemas/output.py` (`GitLabDiscussionPosition`, `GitLabReview`) | New. These are our **input** DTOs — we parse the `GitHubReview`-shaped JSON that `pr-af`'s API returns and translate each comment into a GitLab `position` object. `ScoredFinding`/`ReviewResult` are reused via a pinned dependency (§3), not redefined. |
| `src/pr_af/orchestrator.py` | *(not touched, not forked)* | Consumed only via its HTTP-exposed `review` reasoner, run unmodified inside `pr-af`'s own container. |
| `src/pr_af/app.py` (`review` reasoner, `webhook_github`) | `src/pr_af_gitlab/app.py` (CLI/webhook entrypoint, optional) | Mirrors the *shape* (FastAPI-style route registration, webhook signature verification, async-fire-and-return-execution-id pattern) but talks to the control plane's HTTP API as an external caller — never imports `pr_af.app`. |
| `src/pr_af/config.py` (`AIIntegrationConfig`, `ReviewConfig`) | `src/pr_af_gitlab/config.py` (`GitLabIntegrationConfig`) | New. Same `pydantic.BaseModel` + `Field(default_factory=lambda: os.getenv(...))` idiom, same `from_env`/`from_yaml` classmethod pattern, GitLab-specific env vars (`PR_AF_GITLAB_*`, see §6). |
| `scripts/ci_runner.py` | `scripts/ci_runner.py` | Mirrors shape and control flow (fire → poll `/details` → on success, additionally **fetch `GET /api/v1/executions/{id}`** and hand the `ReviewResult` to `GitLabClient.post_review`). |
| README GH Actions snippet (no file in repo) | `templates/pr-af-review.gitlab-ci.yml` + matching README snippet | Re-implemented for GitLab MR pipelines, shipped for *adopters* to include/copy into the project being reviewed — not run against `pr-af-gitlab` itself; see §8. |
| `.github/workflows/ci.yml` (upstream's own lint/build/test) | `.gitlab-ci.yml` (repo root) | Mirrors this file's *purpose* directly: lints/tests/builds the adapter's own code, nothing more. |
| `docker-compose.yml` | `docker-compose.yml` | Reused near-verbatim: same two services, same env-var names for the AgentField/provider knobs; `pr-af` service's build context points at the pinned upstream ref (§3) instead of a local `Dockerfile`. |
| `Dockerfile` | *(reused unmodified, via pinned upstream build context — not copied into our repo)* | See §3 for why no local copy/extension is needed. |
| `tests/test_resolve_repo.py`, `test_budget_env.py`, etc. | `tests/test_gitlab_client.py`, `tests/test_position_mapping.py`, `tests/test_ci_runner.py` | Same flat-file, `test_<subject>.py` convention; same docstring-explains-the-regression style; prefer real fixtures over mocks where GitLab's contract allows it, mock `httpx` for the GitLab REST calls themselves (§11). |
| `src/pr_af/schemas/gates.py`, `severity.py`, `pipeline.py` | *(reused unchanged, via pinned dependency)* | Purely internal pipeline types; never touched or duplicated. |

### 2.3 The actual CI reference (README snippet, not a shipped file)

For traceability, this is the exact GitHub Actions block from `README.md:271-310` that `templates/pr-af-review.gitlab-ci.yml` re-implements:

```yaml
name: AgentField PR Review
on:
  pull_request:
    types: [labeled]
jobs:
  pr-af-review:
    if: github.event.label.name == 'pr-af'
    runs-on: ubuntu-latest
    permissions:
      contents: read
      pull-requests: write
    steps:
      - uses: actions/checkout@v4
        with: { repository: Agent-Field/pr-af, path: pr-af }
      - working-directory: ./pr-af
        env: { OPENROUTER_API_KEY: ..., GH_TOKEN: ${{ secrets.GITHUB_TOKEN }} }
        run: |
          docker compose up -d
          sleep 15
      - working-directory: ./pr-af
        env: { PR_URL: ${{ github.event.pull_request.html_url }} }
        run: python3 scripts/ci_runner.py
```

Two things this reference confirms for us: (1) the whole stack is stood up **inside the CI job**, torn down with it — matching the brief's non-goal of no persistent GitLab-side service; (2) posting happens because `pr-af`'s own container calls `GitHubClient.post_review` using `GH_TOKEN`, which — per §2.1 — has no GitLab equivalent seam. Our `.gitlab-ci.yml` follows the same "stand up the stack, fire, wait" shape, but adds an explicit result-fetch-and-post step our GitLab CI script owns (§8).

---

## 3. Integration strategy

> **Amendment (see `.claude/steps/002-dockerfile-claude-code-and-anthropic-routing/`):** the "no modified Docker image" part of this recommendation was revised after implementation. `PR_AF_PROVIDER=claude-code` turned out to need `agentfield[harness-claude]`, which upstream's own `Dockerfile` doesn't install — a real gap, not a preference — so this repo now maintains its own `Dockerfile` (fetches `pr-af`'s pinned-commit source, adds that one dependency, swaps in a runtime-override entrypoint). `pr-af`'s own source is still never patched or forked. The rest of this section (HTTP-driven composition, pinned ref, no submodule) is unchanged. Left as originally written below, not rewritten, per this repo's `.claude/README.md` convention of not editing history.

**Recommendation: HTTP-driven composition against a pinned upstream ref — no submodule, no forked source tree, no modified Docker image.**

Concretely:
1. `docker-compose.yml` in our repo brings up `agentfield/control-plane:latest` plus a `pr-af` service whose build context is `pr-af`'s own repository at a **pinned tag or commit SHA**, via Docker Compose / BuildKit's native remote git build context (`build: context: https://github.com/Agent-Field/pr-af.git#<pinned-ref>`). This requires zero local copy of `pr-af`'s source and zero Dockerfile changes — Docker clones and builds it directly. (Verify the GitLab Runner's Docker executor supports BuildKit remote contexts — see §13.)
2. Our adapter's `pyproject.toml` depends on `pr-af @ git+https://github.com/Agent-Field/pr-af.git@<same-pinned-ref>`, used **exclusively** to import its Pydantic output schemas (`ScoredFinding`, `GitHubReview`, `GitHubComment`, `ReviewResult`) for typed parsing of the JSON the control plane API returns. We never import `orchestrator.py`, `app.py`, or `github/client.py` from this dependency — only `pr_af.schemas.output`.
3. Everything else — driving the review, posting to GitLab — happens in our own process, entirely outside `pr-af`'s container, talking only over its already-public HTTP API.

**Why this beats the brief's other three options, given what investigation found:**
- *Pip dependency alone* (importing `pr-af` as a library and calling `ReviewOrchestrator` directly in-process) is foreclosed by §2.1's finding: `orchestrator.py` hardcodes `GitHubClient()` with no injection point. Making `ReviewOrchestrator` post to GitLab in-process would require forking it — an explicit non-goal. Using the pip dependency **only for schema types**, as above, sidesteps this while still satisfying "reuse the real shapes, don't hand-copy field definitions."
- *Git submodule* solves the same "need pr-af's source present to build its image" problem the remote build context already solves, with more footguns (detached HEAD, forgotten `submodule update`, doubled disk in CI). No advantage over a pinned URL fragment; not recommended.
- *Docker base image (`FROM` pr-af's image, adding a GitLab layer on top)* would only be justified if we needed our `GitLabClient` running **inside** the same container/process as the orchestrator — which, again, isn't wireable without forking `orchestrator.py`. Since Phase A/B posting happens from our own external process instead, there's nothing to add to that image; extending it would be complexity with no corresponding capability gain.
- *Upstream-ready external* (structuring `GitLabClient` so it could later be upstreamed as a sibling to `GitHubClient` behind a provider switch) remains a **good long-term aspiration**, not a blocker now: because we mirror `GitHubClient`'s shape method-for-method (§5), the eventual upstream PR would mostly be "move this file into `pr_af/gitlab/` and add an `if self.input.mr_url` branch in `orchestrator.py`" — a natural continuation, not a rewrite. Noted as a non-blocking future path, not something this plan depends on.

This satisfies all four of the brief's criteria: upstream stays completely pristine (zero commits, zero forked files); the "mirror" principle is honored (schema types are the *actual* upstream Pydantic models, not hand-mirrored copies, wherever reuse is possible); the CI story is simple (one `docker compose up`, same as upstream's own reference); and the pin point (one git ref, used twice) is trivially reproducible.

---

## 4. Architecture

This runs entirely in **the pipeline of the project being reviewed** (the adopter's repo), via the included template from §8 — not in `pr-af-gitlab`'s own pipeline:

```
Adopter project's GitLab MR pipeline (label-gated, job from our included template)
        │
        ▼
.gitlab-ci.yml job
  ├─ docker compose up -d   (agentfield control-plane + pinned pr-af image)
  ├─ wait for /health
  ├─ scripts/ci_runner.py:
  │     1. POST /api/v1/execute/async/pr-af.review
  │          { repo_path: <CI checkout>, base_ref: $CI_MERGE_REQUEST_DIFF_BASE_SHA,
  │            head_ref: $CI_COMMIT_SHA, dry_run: true, output_format: "markdown" }
  │     2. poll /api/ui/v1/executions/{id}/details  until succeeded/failed
  │     3. GET /api/v1/executions/{id}               → full ReviewResult JSON
  │     4. GitLabClient.post_review(review_result)   → GitLab discussions API
  └─ docker compose down
        │
        ▼
GitLab MR: line-anchored discussions (Phase B) or one summary note (Phase A)
```

Because Mode 3 (`repo_path`/`base_ref`/`head_ref`) needs **no fetch step of any kind** — the CI runner's own checkout *is* the repo — there is no `fetch_mr`-equivalent in the critical path at all. The only place a `fetch_mr` could matter is optional metadata parity (title/description/author for the summary note) — see §12.3.

`dry_run=true` is essential: it stops `orchestrator.py`'s hardcoded `GitHubClient().post_review(...)` call from ever firing (there's no `pr_url`, so it wouldn't fire anyway in Mode 3 — see `_generate_output`'s guard `if post_to_github and not self.input.dry_run and self.input.pr_url`). Since Mode 3 never sets `pr_url`, that guard is already false regardless of `dry_run`; we set it anyway for explicitness and to guard against a future upstream change that broadens the posting condition.

---

## 5. `GitLabClient` design

Mirrors `src/pr_af/github/client.py` 1:1 in shape, `async`/`httpx` style, and print-based logging conventions (`print(f"[PR-AF-GITLAB] ...", flush=True)`).

| `GitHubClient` method | `GitLabClient` method | Mapping / divergence |
|---|---|---|
| `parse_pr_url(url) -> (owner, repo, number)` | `parse_mr_url(url) -> (project_path, mr_iid)` | GitLab project paths are `group/subgroup/project` (arbitrary depth), not a fixed two-segment `owner/repo` — regex must capture a greedy path segment before `/-/merge_requests/<iid>`. |
| `_headers()` / `_headers_for_repo()` | `_headers()` | GitLab uses a single `PRIVATE-TOKEN: <token>` header (or `Authorization: Bearer <token>` for OAuth/CI job tokens) — no per-repo installation-token exchange; GitLab has no direct equivalent of GitHub App installation tokens, so `_get_installation_token`/`_generate_app_jwt` have **no GitLab counterpart** and are dropped, not mirrored. Documented divergence. |
| `fetch_pr(pr_url) -> GitHubPRData` | `fetch_mr(mr_url) -> GitLabMRData` (optional, §12.3) | `GET /projects/:id/merge_requests/:iid` for metadata, `GET .../changes` for the diff/changed files, `GET .../commits` for messages. Not on the critical path (Mode 3 supplies the diff already) — see §12.3 for whether to build this at all. |
| `post_review(owner, repo, pr_number, review, commit_sha) -> dict` | `post_review(project_id, mr_iid, review, diff_refs) -> dict` | The core of this adapter. See §5.1. |
| `clone_repo(owner, repo, target_dir, shallow) -> str` | `clone_repo(project_path, target_dir, shallow) -> str` | Tokenized HTTPS clone: `https://oauth2:{token}@gitlab.example.com/{project_path}.git` (PAT/OAuth) or `https://gitlab-ci-token:{CI_JOB_TOKEN}@...` (job token, scope permitting — §7.1). Mirrors the shape; **not needed for the CI critical path** (the runner's own checkout is the repo), kept only for the optional webhook-triggered flow (§12.6) where a review is fired outside a pipeline and needs its own checkout. |

### 5.1 The `position` object mapping (the hard part)

`pr-af`'s `GitHubReview.comments` is a list of `GitHubComment{path, line, side, body}` (`schemas/output.py:60-66`). GitLab's `POST /projects/:id/merge_requests/:iid/discussions` needs a `position` object instead. Mapping:

| `GitHubComment` field | GitLab `position` field(s) | Notes |
|---|---|---|
| `path` | `position[new_path]` (and `position[old_path]` if renamed) | For `side="RIGHT"` (added/context line), `new_path` is authoritative; `old_path` mirrors it unless the file was renamed (rename info isn't in `GitHubComment` today — see below). |
| `line` | `position[new_line]` if `side="RIGHT"`; `position[old_line]` if `side="LEFT"` | GitLab requires the *other* side's line field to be omitted/null for single-sided comments on unchanged context — verify exact rule against the live API (§13). |
| *(none — computed)* | `position[position_type] = "text"` | Constant for inline text comments (image diffs use `"image"`, not applicable here). |
| *(none — fetched)* | `position[base_sha]`, `position[start_sha]`, `position[head_sha]` | **Not derivable from `GitHubComment` at all.** Must come from the MR's `diff_refs` object (`GET /merge_requests/:iid` → `diff_refs.{base_sha, start_sha, head_sha}`), fetched once per run — **not** reliably substitutable with CI predefined variables alone, since `start_sha` in particular has no direct CI-variable equivalent and `base_sha` semantics can drift from `CI_MERGE_REQUEST_DIFF_BASE_SHA` across GitLab versions during target-branch rebases. Recommend: one lightweight `GET /merge_requests/:iid` call at the start of `post_review` to fetch `diff_refs` authoritatively, rather than trusting CI variables for this specific triple. |
| *(body)* | `body` (top-level, unchanged) | Direct passthrough. |
| `event` (APPROVE/COMMENT/REQUEST_CHANGES) | *(no direct GitLab equivalent)* | See divergence below. |

**Findings that can't be cleanly anchored** (line outside the current diff hunks, renamed/deleted file edge cases, or a `position` API rejection) fall back to a plain MR-level note (`POST /projects/:id/merge_requests/:iid/notes`) prefixed with the file/line reference in text, mirroring `pr-af`'s own philosophy of never silently dropping a finding (it already filters/logs `skipped_path`/`skipped_range` counts in `_generate_output`).

**Documented divergences (GitHub → GitLab), beyond the position mapping:**
- **Review "event" has no GitLab equivalent.** GitHub's PR Review API bundles an approval/request-changes/comment *state* with the inline comments in one atomic call. GitLab's Discussions API has no such bundling — approval is a separate Approvals API (often disabled/limited on Community Edition without required-approvers config) and there's no "request changes" primitive at all. **Recommendation:** map `REQUEST_CHANGES`/`COMMENT`/`APPROVE` to (a) the summary note's wording and an emoji/severity header, plus (b) optionally applying/removing a label (e.g. `pr-af::blocking-findings`) the team can gate merge on via a push rule or merge check — not a GitLab "review state." This must be called out to users of the adapter so expectations match GitLab's actual primitives, not GitHub's.
- **"Pull request" → "merge request", "review" → "discussion"** — purely terminological, propagated consistently through class/method/schema names (`GitLabMRData`, `post_review` posting *discussions*, etc.).
- **No GitHub-App-style installation auth** — see table above; GitLab's closest analogue (Project/Group Access Tokens) is a plain PAT-shaped token, not a short-lived JWT exchange, so `GitLabClient` has no `_get_installation_token` equivalent at all — a structural simplification, not a gap.
- **Renames**: `GitHubComment` carries no `previous_path`(unlike `ChangedFile`, which does) — if a finding lands on a renamed file, GitLab's `position` needs both `old_path` and `new_path` to differ correctly. Since we're translating `ScoredFinding`/`GitHubComment` (which only carries `path`), we'll cross-reference `pr_data.changed_files[].previous_path` (available in the full `ReviewResult.metadata` payload) when building positions, falling back to `old_path == new_path` when no rename is recorded.

### 5.2 Phasing (from the brief, reaffirmed)

- **Phase A**: `output_format=markdown`, `dry_run=true`. `GitLabClient.post_review` posts **one summary note** (`POST .../notes`) built from `ReviewResult.review.body` plus a rendered findings list. No `position` objects, no diff_refs fetch. Gets the whole pipeline (CI trigger → pr-af run → something visible on the MR) working end-to-end fastest.
- **Phase B**: line-anchored discussions via the full `position` mapping in §5.1, reaching GitHub-inline-comment parity.

---

## 6. Provider wiring

**Confirmed working, code-grounded, for `.harness()` calls** (planner/reviewer/cross_ref/adversary — the bulk of review reasoning and cost):

```
PR_AF_PROVIDER=claude-code
PR_AF_MODEL=claude-sonnet-4-5          # or whichever Anthropic model id is approved
ANTHROPIC_API_KEY=<masked CI variable>
```

`config.py::AIIntegrationConfig.provider_env()` explicitly whitelists and forwards `ANTHROPIC_API_KEY` into the harness subprocess environment (`config.py:322-334`), and `app.py`'s `HarnessConfig(provider=_ai_config.provider, model=_ai_config.harness_model, env=_ai_config.provider_env(), ...)` passes it straight through to the `claude-code` harness backed by `claude_agent_sdk`. This path requires **no code change** — only environment variables in `docker-compose.yml`/CI variables.

**Not resolved by environment variables alone** — the `.ai()` path (`app.py:45-49`):

```python
ai_config=AIConfig(
    model=_ai_config.ai_model,
    api_key=os.getenv("OPENROUTER_API_KEY", ""),
    api_base="https://openrouter.ai/api/v1",
)
```

This is a literal, hardcoded construction — not conditioned on `PR_AF_PROVIDER` at all. It backs `intake_phase`'s fast gate, `coverage_gate`, `polish_comments`, and `classify_findings` (the merge-blocker gate). See §7.2 for the full risk analysis and the decision this requires from Jeff before Phase A — **this is not a "set an env var and it works" situation**, and the plan does not pretend otherwise.

---

## 7. Auth & security

### 7.1 Token model for posting discussions

`CI_JOB_TOKEN`'s ability to call the Discussions/Notes API varies by GitLab version and instance configuration (it gained broader REST API scope over several releases, but historically excluded posting notes/discussions on MRs outside the same project, and Community Edition admins can restrict its API scope entirely via `CI_JOB_TOKEN` allowlist settings). **Recommendation: use a dedicated Project or Group Access Token** (bot-like, scoped to `api` + the specific project/group, not a personal PAT tied to a human account) stored as a **masked + protected** CI variable (`PR_AF_GITLAB_TOKEN`). This sidesteps the `CI_JOB_TOKEN` scope question entirely and gives an auditable, revocable identity ("PR-AF Bot") for posted discussions, matching how GitHub's flow uses a bot-flavored token (`GH_TOKEN`/GitHub App) rather than a human PAT. **Verify against the actual instance** whether `CI_JOB_TOKEN` scope has since been widened enough to drop this requirement — flagged in §13.

> **Amendment:** now confirmed, not just flagged — GitLab's own CI/CD job token docs state job tokens get read-only (`GET`) access to the Notes API, never `POST`. `CI_JOB_TOKEN` cannot post discussions/notes under any GitLab version or instance config. The Project/Group Access Token above is required, not just recommended; `config.py`/README updated accordingly.

### 7.2 The OpenRouter/`.ai()` trust-boundary risk — decision required

> **Amendment (see `.claude/steps/002-dockerfile-claude-code-and-anthropic-routing/`):** resolved, with a simpler mechanism than this section anticipated. Reading the actual `agentfield` package showed `AIConfig.api_base`/`api_key` are optional pass-throughs to litellm — when `api_base` is left unset, litellm's own `anthropic/<model>`-prefixed routing talks to Anthropic directly. `docker/entrypoint_wrapper.py` reassigns `pr_af.app.app.ai_config` at container startup (a plain mutable attribute on `agentfield.Agent`, confirmed read fresh per `.ai()` call) whenever `ANTHROPIC_API_KEY` is set — no source patch to `pr-af` at all, and no OpenRouter exposure once that key is configured. Falls back to the original OpenRouter behavior when it isn't set. The trade-off analysis below is left as originally written for context, not rewritten.

This is the most consequential finding in this investigation. Summarized:

- `merge_gate.py::_build_user_prompt(finding)` sends `finding.body`, `finding.evidence` ("quote the code from BOTH ends," per the consistency-verify prompt that produces these findings), and `finding.suggestion` (a literal suggested code fix) to `.ai()` — i.e., genuine source-code-derived content, not just metadata (`merge_gate.py:81-99`, confirmed by direct read).
- `polish.py::_polish_one` sends the **entire comment body** — which is built from the same finding fields — to `.ai()` for rewriting (`polish.py:29-40`, confirmed by direct read).
- Both routes go through the hardcoded `AIConfig(api_base="https://openrouter.ai/api/v1", api_key=os.getenv("OPENROUTER_API_KEY", ""))` in `app.py`, regardless of `PR_AF_PROVIDER`.
- By contrast, `intake_phase`'s `.ai()` gate call sends PR **metadata only** (title, description truncated to 500 chars, labels, author, file/language counts, first 5 commit messages — `harnesses.py:250-261`) — no diff lines, no quoted code. `coverage_gate` sends cluster **names/descriptions and risk-surface labels** (LLM-authored summaries), not raw source. These two are lower-risk but not zero-risk, since PR descriptions/commit messages are still user-authored content leaving the org boundary.
- **This conflicts with the stated premise** that "the data-governance decision for sending code to the Anthropic API is already made and approved" if that approval is read as *Anthropic and no one else*: `polish_comments` and `classify_findings` (both **on by default** — `CommentConfig.polish_enabled = True`, `merge_gate_enabled = True`, neither exposed as an env var or a `ReviewInput` field) will send code-bearing content to OpenRouter on every review unless something changes.
- **Neither pass has an environment-variable off-switch.** `polish_enabled`/`merge_gate_enabled` are plain Python `bool` defaults in `config.py::CommentConfig`, not `Field(default_factory=lambda: os.getenv(...))` like `post_worthiness_gate` is. `ReviewConfig.from_input` also doesn't forward any `ReviewInput` field into them. There is no config-only way to disable this behavior on the current pinned version.

**Options, with a recommendation:**
1. **(Recommended) Contribute a small, generic upstream patch** — make `.ai()`'s provider/base/key resolution env-driven the same way `.harness()`'s already is (e.g., when `PR_AF_PROVIDER=claude-code`, route `.ai()` through `anthropic/`-prefixed litellm model strings using `ANTHROPIC_API_KEY`, falling back to today's OpenRouter behavior when unset — fully backward compatible), and additionally expose `PR_AF_POLISH_ENABLED`/`PR_AF_MERGE_GATE_ENABLED` env toggles mirroring the existing `PR_AF_POSTWORTHINESS_GATE` pattern already in the codebase. This is a few lines, framed as a generic "provider-agnostic `.ai()` routing" improvement useful to any `pr-af` deployment, not a GitLab-specific fork — ideal to propose as an upstream PR to `Agent-Field/pr-af` so our repo never needs to carry a diff at all.
2. **Interim mitigation, no upstream change**: set `PR_AF_POLISH_ENABLED`/equivalent to off is *not currently possible* without (1). The only environment-only lever available today is disabling these features by **not** running Phase B features that depend on the `.ai()` path being fully out-of-scope — but `polish_enabled`/`merge_gate_enabled` are unconditional on any successful run, so there is no environment-only mitigation. This must be surfaced to Jeff plainly: **either accept (1) as a prerequisite, accept the OpenRouter exposure of finding text/evidence/suggestions as a documented residual risk, or re-scope the review to skip these two passes some other way** (e.g., a fork limited to two `True`→env-gated defaults, which is a much smaller, easier-to-defend deviation than modifying `orchestrator.py`, if Jeff prefers not to wait on an upstream PR).
3. **Do not** route Anthropic models through OpenRouter as a workaround (option (c) considered and rejected) — the request still transits OpenRouter's infrastructure, which almost certainly re-opens the "no other third party" requirement in the brief's own security section (§5.5), rather than satisfying it.

**This blocks a clean "Anthropic-only" Phase A** as literally specified until Jeff decides among these. Recorded as Open Decision §12.7 and Risk §13.1.

### 7.3 General security requirements (NEN 7510/7513-aligned)

- **Least-privilege token scope**: the Project/Group Access Token (§7.1) scoped to exactly one project (or the group housing it), `api` scope only, no `write_repository`/admin scopes beyond what discussion-posting needs. Set an expiry and rotate.
- **Secrets only via masked + protected GitLab CI/CD variables**: `PR_AF_GITLAB_TOKEN`, `ANTHROPIC_API_KEY`, and (until §7.2 is resolved) `OPENROUTER_API_KEY` — never committed, never echoed. `docker compose` reads them from the job environment exactly as upstream's own reference workflow does.
- **Pinned dependency refs and pinned images**: `pr-af` build context pinned to a tag/SHA (§3), `agentfield/control-plane` pinned to a digest rather than `:latest` (upstream's own `docker-compose.yml` uses `:latest` with `pull_policy: always` — **deliberately not mirrored here**; floating tags in a compliance-sensitive CI pipeline are a documented, intentional divergence from upstream for this one line).
- **Trust boundary**: code/diffs leave the GitLab runner only to (a) the Anthropic API for `.harness()` calls, confirmed by code, and (b) — pending §7.2's resolution — potentially OpenRouter for the two `.ai()` passes. No other third party is contacted; `pr-af` makes no other outbound network calls in its core pipeline (confirmed: only `github/client.py`, the harness subprocess, and litellm's `.ai()` calls touch the network, per the import sweep in §2.1).
- **Log hygiene**: `pr-af`'s own logging (`print(..., flush=True)`) includes PR/MR numbers, finding counts, and cost — not tokens or full diffs, from direct read of every `print` call in `orchestrator.py`/`github/client.py`. Our own `ci_runner.py`/`GitLabClient` logging should follow the same pattern: never log the `PRIVATE-TOKEN`/bearer header value, log finding *counts* and *titles*, not full evidence/suggestion bodies (which could otherwise leak snippets of proprietary code into CI job logs, which may themselves be less access-controlled than the source repo). **Open question for Jeff**: whether job-log visibility of findings text (as opposed to token values, which are never logged either way) is acceptable in this environment — flagged in §12.
- **Audit trail**: the AgentField control plane already retains execution records (`/api/v1/executions/{id}`) queryable after the fact — sufficient for now; no additional adapter-side audit logging is proposed unless the healthcare-compliance context requires a longer retention window than the control plane provides (unverified — flagged in §13).

---

## 8. CI design

Two distinct `.gitlab-ci.yml`-shaped artifacts, matching the distinction upstream already draws between its own `ci.yml` (lint/build/test `pr-af` itself) and the README-only review-trigger snippet (runs in *other* repos):

- **`pr-af-gitlab`'s own `.gitlab-ci.yml`** (repo root): lints and tests the adapter's own code — ruff, `pytest tests/`, a `docker compose config`/build sanity check. Mirrors upstream `ci.yml`'s shape (lint job, docker-build job). Nothing in it reviews merge requests.
- **`templates/pr-af-review.gitlab-ci.yml`** (shipped in this repo, meant to run in *adopter* projects): the actual review-trigger job, added to the project being reviewed either by copy-pasting the README snippet or — the recommended, more GitLab-idiomatic route, see §12.8 — via a pinned `include:`:

  ```yaml
  # In the ADOPTER project's own .gitlab-ci.yml:
  include:
    - project: '<group>/pr-af-gitlab'
      ref: 'v1.0.0'                 # pin to a released tag, same "pin a ref" discipline as §3
      file: '/templates/pr-af-review.gitlab-ci.yml'
  ```

  Template content:

```yaml
review:
  stage: review
  rules:
    - if: '$CI_MERGE_REQUEST_LABELS =~ /pr-af/'
      when: on_success
  image: docker:24-cli
  services:
    - docker:24-dind
  variables:
    DEPTH: ${PR_AF_DEPTH:-standard}   # quick|standard|deep, overridable per-MR via a CI variable or label suffix
  before_script:
    - docker compose -f docker-compose.yml up -d
    - ./scripts/wait_for_health.sh    # polls control-plane + pr-af /health
  script:
    - python3 scripts/ci_runner.py
  after_script:
    - docker compose down
  variables:
    ANTHROPIC_API_KEY: $ANTHROPIC_API_KEY       # masked+protected
    PR_AF_GITLAB_TOKEN: $PR_AF_GITLAB_TOKEN      # masked+protected
    PR_AF_PROVIDER: claude-code
    PR_AF_MODEL: claude-sonnet-4-5
```

- **Trigger/gate**: MR pipeline, gated on the `pr-af` label being present (`CI_MERGE_REQUEST_LABELS` predefined variable), mirroring the upstream README's `github.event.label.name == 'pr-af'` gate as closely as GitLab's rule syntax allows.
- **CI-variable mapping** (candidates, **flagged for version verification** — §13): `CI_MERGE_REQUEST_DIFF_BASE_SHA` (→ `base_ref`), `CI_COMMIT_SHA` (→ `head_ref`, the MR's current HEAD in a merge-request pipeline), `CI_MERGE_REQUEST_IID`, `CI_PROJECT_ID`, `CI_API_V4_URL` (base URL for `GitLabClient`'s REST calls). These four have been broadly stable across recent GitLab versions but should be confirmed against the actual instance version rather than assumed.
- **`scripts/ci_runner.py`** (GitLab-flavored): fires the async execution with `repo_path=$CI_PROJECT_DIR`, `base_ref`, `head_ref`, `dry_run=true`, `output_format=markdown` (Phase A) or the full comment path (Phase B); polls exactly like upstream's runner; on success, additionally calls `GET /api/v1/executions/{id}` and hands the result to `GitLabClient.post_review`.
- **Depth control**: `ReviewInput.depth` already supports `auto|quick|standard|deep` with size-based auto-escalation (`_resolve_depth`/`_escalate_depth` in `orchestrator.py`) — no adapter work needed beyond passing through a `depth` CI variable (default `auto`, letting `pr-af`'s own line-count heuristic decide), with an optional per-MR override via a second label (`pr-af::deep`) parsed in the CI job.

---

## 9. Phased milestones

**Phase A — summary note (walking skeleton)**
- `templates/pr-af-review.gitlab-ci.yml` (added to a low-stakes test repo, per §12.8) + `docker-compose.yml` bring up the stack and tear it down cleanly on both success and failure.
- `scripts/ci_runner.py` fires a Mode-3 review (`dry_run=true`) and retrieves the full `ReviewResult`.
- `GitLabClient.post_review` posts **one** MR-level note summarizing findings (counts by severity, top items) — no `position` objects yet.
- **Acceptance**: with the template included/copied into a low-stakes internal test repo, opening an MR there with the `pr-af` label produces exactly one note on the MR within the expected review-duration window (35-50 min per upstream's own README caveat), with no secrets in job logs, and the job exits 0/1 correctly on pipeline success/failure.

**Phase B — line-anchored discussions**
- `GitLabClient.post_review` fetches `diff_refs` and posts one `position`-anchored discussion per in-diff finding, falling back to an MR-level note for anything that can't be cleanly anchored (§5.1).
- **Acceptance**: a test MR with findings on added lines, removed lines, and a renamed file each produce correctly anchored discussions, verified against a live low-stakes GitLab project (not just mocked).

**Phase C — hardening**
- Resolve §7.2 (either the upstream `.ai()` env-routing patch lands, or Jeff accepts a documented interim posture).
- Confirm §7.1's token-scope decision against the real instance.
- Pin `agentfield/control-plane` to a digest; confirm the BuildKit remote-context assumption (§3) against the actual GitLab Runner executor.
- **Acceptance**: a from-scratch run on a clean runner, with no floating tags anywhere in the compose file, completes successfully; the security section's open items are all either resolved or explicitly accepted in writing by Jeff.

---

## 10. Testing strategy

- **Mirror `pr-af`'s convention**: flat `tests/test_<subject>.py` files, pytest, docstrings stating the regression/contract under test, favoring real fixtures (e.g., a local git repo standing in for a GitLab remote) over mocks where the shape allows — matching `test_resolve_repo.py`'s own style.
- **`GitLabClient` unit tests**, no live instance required: mock `httpx.AsyncClient` responses for `parse_mr_url` (URL shape edge cases: nested groups, MR vs. issue URLs), `post_review`'s `position` construction (assert the exact JSON payload per §5.1's mapping table, including the fallback-to-note path), and error handling (4xx from a bad `diff_refs`, 5xx retry behavior if we mirror `GitHubClient`'s retry loop).
- **`ci_runner.py` tests**: mock the two control-plane endpoints (`/execute/async/...`, `/executions/{id}`) to verify polling/timeout/failure-propagation logic, mirroring the spirit of `test_budget_env.py`/`test_budget_message.py`'s environment-driven-config testing style.
- **Manual end-to-end test plan** (required before Phase A is called "done," since Phase A's acceptance criteria explicitly needs a live MR):
  1. Stand up a disposable/low-stakes internal GitLab project.
  2. Open an MR with a handful of deliberately flawed changes (a mix of an added-line bug, a removed-line issue, and a renamed file) against it.
  3. Apply the `pr-af` label; watch the pipeline bring the stack up, run, and tear down.
  4. Confirm the note (Phase A) / discussions (Phase B) appear correctly, that no secrets appear in job logs, and that the job's pass/fail status reflects the review outcome, not just "the container started."
  5. Re-run against the same MR after pushing a fixup commit, to confirm workspace-reuse behavior (mirroring `test_resolve_repo.py`'s reused-workspace regression concern) doesn't stale-review the wrong tree in our own driving code.

---

## 11. Repo scaffold (finalized)

```
pr-af-gitlab/
├── .gitlab-ci.yml                  # lint/test/build THIS repo only — never reviews its own MRs
├── README.md
├── PLAN.md
├── docker-compose.yml              # agentfield control-plane + pr-af (pinned remote build context)
├── pyproject.toml                  # depends on pr-af@<pinned-ref> for schema types only
├── templates/
│   └── pr-af-review.gitlab-ci.yml  # the actual review-trigger job — adopters include/copy this into THEIR repo
├── src/pr_af_gitlab/
│   ├── gitlab/
│   │   ├── __init__.py
│   │   └── client.py               # GitLabClient — mirrors github/client.py
│   ├── schemas/
│   │   ├── __init__.py
│   │   ├── input.py                # GitLabMRData (only if §12.3 says yes)
│   │   └── output.py               # GitLabDiscussionPosition, GitLabReview
│   ├── config.py                   # GitLabIntegrationConfig, mirrors config.py idioms
│   └── app.py                      # optional webhook entrypoint (§12.6)
├── scripts/
│   ├── ci_runner.py                # mirrors upstream scripts/ci_runner.py
│   └── wait_for_health.sh
└── tests/
    ├── test_gitlab_client.py
    ├── test_position_mapping.py
    └── test_ci_runner.py
```

---

## 12. Open decisions (recommendation for each)

**12.1 Repo name.** Recommend **`pr-af-gitlab`** — matches the brief's own working name, unambiguous, greppable, consistent with `pr-af-go`'s naming precedent already established upstream. (`pr-af-gl` is more terse but less discoverable; an org-prefixed variant adds naming noise this adapter doesn't need since it isn't itself an org-branded product.)

**12.2 Integration strategy.** Resolved in §3: HTTP-driven composition against a pinned upstream ref, pip-from-git for schema types only. No submodule, no forked source, no modified image.

**12.3 Add `fetch_mr` for metadata parity, or run purely off the CI checkout?** Recommend **defer / skip for Phase A and B**. Mode 3 needs no fetch at all for the review itself, and the summary note can be built entirely from CI predefined variables (`CI_MERGE_REQUEST_TITLE`, `CI_MERGE_REQUEST_ID`, `CI_PROJECT_PATH` are already available in the job environment without any API call). A `fetch_mr` only earns its keep if we later want `GitLabMRData` fields the CI variables don't cover (e.g., MR description body, existing labels) for a richer summary note — worth adding in Phase C if the Phase A note feels thin, not before.

**12.4 Token model.** Recommend **Project or Group Access Token** over `CI_JOB_TOKEN`, per §7.1 — pending confirmation of `CI_JOB_TOKEN`'s current scope on the actual instance (§13), which could simplify this later but shouldn't be assumed now. **Amendment: resolved, not just recommended.** Confirmed against GitLab's own docs that `CI_JOB_TOKEN` never gets write access to the Notes API — the access token is required, no instance-config scenario makes `CI_JOB_TOKEN` viable for posting.

**12.5 Trigger model.** Recommend **MR label**, mirroring upstream's own trigger mechanism exactly (`pr-af` label) rather than diverging to "every MR" (would run an expensive 35-50 min review unconditionally, including on trivial MRs) or pure `when: manual` (loses the "just works once labeled" ergonomic upstream deliberately optimized for). Depth scaling by size (§8) complements this rather than replacing it.

**12.6 MR-comment webhook (mirroring GitHub's `@mention` handler).** Recommend **defer**. It's a genuinely separable feature (upstream's own `webhook_github` is independent of the CI-triggered path) with its own auth/exposure surface (a public-facing webhook endpoint needs its own hosting decision, unlike the CI job which is ephemeral) — worth a follow-up plan once Phase A/B are proven, not bundled into the initial scope.

**12.7 The `.ai()`/OpenRouter trust boundary (§7.2).** This is the one decision that isn't a preference but a blocker: recommend Jeff choose between (1) sponsoring/accepting a small upstream patch to make `.ai()` provider-routing env-driven (preferred — keeps us fork-free and benefits any other Anthropic-only `pr-af` deployment), or (2) accepting the current OpenRouter exposure of finding text as a documented residual risk for Phase A, revisited before Phase C's hardening acceptance criteria can be called met. **Amendment: resolved via option (3), not listed here at the time** — a runtime override (`docker/entrypoint_wrapper.py`) reroutes `.ai()` to Anthropic directly without patching `pr-af`'s source or waiting on an upstream PR. See §7.2's amendment and `.claude/steps/002-dockerfile-claude-code-and-anthropic-routing/`.

**12.8 How adopters pull in the review-trigger job: copy-paste README snippet vs. a pinned `include:`.** Recommend **ship both, lead with `include:`** — a GitLab `include: - project: '<group>/pr-af-gitlab', ref: '<tag>', file: '/templates/pr-af-review.gitlab-ci.yml'` (§8) is more idiomatic than GitHub Actions' copy-paste-a-workflow-file convention, gives every adopter a single source of truth they can bump by changing one `ref:`, and mirrors the "pin one ref, use it consistently" discipline already used for the `pr-af` build context (§3). The one caveat: `include: project:` requires the adopter's runner to have network/permission access to fetch from wherever `pr-af-gitlab` is hosted, which may not hold for network-isolated or air-gapped GitLab instances — for those, the plain copy-paste README snippet (functionally identical, just duplicated instead of included) remains the fallback. Both are cheap to maintain from the same template file, so shipping both costs little.

---

## 13. Risks & unknowns (things this pass could not verify without live access)

1. **The `.ai()`/OpenRouter hardcoding (§7.2)** is the highest-priority item — confirmed by source, but the *remedy* (an upstream patch, or `agentfield`'s own possible env-override support that this investigation had no access to verify) needs the `agentfield` package's actual source, which lives outside `pr-af`'s repo entirely.
2. **`SUPPORTED_PROVIDERS = {"claude-code", "codex", "gemini", "opencode"}`** — not found in `pr-af`'s own source; must be verified against the installed `agentfield>=0.1.84` package to confirm `claude-code` is genuinely accepted end-to-end (the string is used consistently in `pr-af`'s own config/tests, but the harness factory that validates it lives in `agentfield`).
3. **GitLab Discussions/`position` API exact contract** — the field names and required-field rules in §5.1 are drawn from GitLab's general API shape as of recent releases; **must be verified against the specific GitLab CE version in use**, since discussion/position semantics (particularly around context-line comments and multi-line comment ranges) have shifted across releases.
4. **`CI_JOB_TOKEN` scope for posting discussions** — genuinely version- and instance-configuration-dependent; §7.1's recommendation to use a dedicated access token sidesteps needing to know this precisely, but confirming it could simplify the token model later.
5. **CI predefined variable availability/semantics** (`CI_MERGE_REQUEST_DIFF_BASE_SHA` etc.) — listed as "candidates" deliberately; confirm against the instance version before relying on them in `.gitlab-ci.yml`.
6. **BuildKit remote git build-context support on the GitLab Runner's Docker executor** (§3) — needs a modern Docker/Compose version; if unavailable, fall back to an explicit `git clone` step before `docker compose build` (a one-line change, not a strategy change).
7. **Audit-trail retention** — whether the AgentField control plane's execution history retention window satisfies any healthcare-compliance (NEN 7510/7513) audit requirement Jeff's org may have; not verifiable from `pr-af`'s source alone.
8. **Log-hygiene acceptability of findings text in CI job logs** (§7.3) — a policy question for Jeff's org, not a technical unknown.

---

*End of plan. Awaiting review before any scaffolding, dependency pinning, or code is written.*
