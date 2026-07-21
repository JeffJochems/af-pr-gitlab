# Fix claude-code provider + Anthropic-direct `.ai()` routing

## Context

Live-instance research (GitLab 17.7 CE docs + the actual downloaded `agentfield` PyPI package, not guesswork) turned up two problems with what's currently committed in `docker-compose.yml`:

1. **`PR_AF_PROVIDER=claude-code` doesn't actually work.** `docker-compose.yml` defaults to it, but `ClaudeCodeProvider` (confirmed by reading `agentfield/harness/providers/claude.py` and `_factory.py` from the real 0.1.111 wheel) needs the `claude_agent_sdk` package, installed via the `agentfield[harness-claude]` extra. `pr-af`'s own `Dockerfile` only installs plain `agentfield>=0.1.84` — no `claude-agent-sdk`. As committed today, a review would fail at the first `.harness()` call with `HarnessProviderUnavailable`.
2. **The OpenRouter hardcoding (`src/pr_af/app.py:45-49`) has a much simpler fix than PLAN.md §7.2 assumed.** `AIConfig.api_key`/`api_base` are optional pass-throughs to litellm (confirmed by reading `agentfield/types.py`'s `get_litellm_params` — `if self.api_base: params["api_base"] = ...`, only set when non-None). When `api_base` is left unset, litellm's own `anthropic/<model>`-prefixed routing talks to Anthropic directly using `ANTHROPIC_API_KEY` — the same mechanism `.harness()` already relies on. `app.py` currently sets `api_base="https://openrouter.ai/api/v1"` unconditionally, which is what forces every `.ai()` call through OpenRouter regardless of model string.

Jeff already decided (this session): fix both via **a small local patch in our own image now**, specifically **rerouting `.ai()` to Anthropic** rather than disabling the polish/merge-gate passes it powers.

**Better mechanism than "patch pr-af's source", found during this planning pass:** `agentfield.Agent` stores `ai_config` as a plain mutable attribute (confirmed: `agent.py:755` — `self.ai_config = ai_config if ai_config else AIConfig.from_env()`), read fresh on every `.ai()` call (confirmed: `agent_ai.py` reads `self.agent.ai_config` per-call throughout, e.g. `final_config = self.agent.ai_config.copy(deep=True)`), not baked into a closure at construction. So instead of a unified-diff patch against `app.py`'s exact source lines (fragile against upstream drift, needs the `patch` tool), we can reassign `pr_af.app.app.ai_config` **after import, before the server starts** — zero modification to pr-af's actual file, and far more robust (depends only on `Agent` still exposing a mutable `.ai_config` attribute, not on exact surrounding source lines matching).

This still requires our own `Dockerfile` (the `agentfield[harness-claude]` dependency addition can't be done at runtime — it's a real package install), revising PLAN.md §3's "zero Dockerfile, pure remote build context" integration strategy. That's a deliberate, narrow, well-justified deviation — not scope creep — and gets documented as such, not silently folded into the original plan.

## Approach

### 1. New `Dockerfile` (repo root)

Mirrors `pr-af`'s own `Dockerfile` (both stages) almost line-for-line, with three deltas:
- **Source fetch**: instead of `COPY pyproject.toml README.md ./` / `COPY src/ src/` (which assume `pr-af`'s own repo as build context), fetch the pinned commit via GitHub's tarball endpoint: `ADD https://github.com/Agent-Field/pr-af/archive/6b82efc8ade7cd48420ecd6de59eeb1cb80d3b49.tar.gz /tmp/pr-af.tar.gz`, then `RUN mkdir -p /tmp/pr-af-src && tar -xzf /tmp/pr-af.tar.gz --strip-components=1 -C /tmp/pr-af-src`.
- **Dependency addition**: builder-stage pip install line changes `"agentfield>=0.1.84"` → `"agentfield[harness-claude]>=0.1.84"`; everything else in that list (`hax-sdk`, `pydantic`, `httpx`, `python-dotenv`, `fastapi`, `uvicorn`, `PyJWT[crypto]`) stays identical, installing from `/tmp/pr-af-src` instead of `.`.
- **Entrypoint**: runtime stage additionally `COPY docker/entrypoint_wrapper.py /app/entrypoint_wrapper.py`, and `CMD` changes from `["python", "-m", "pr_af.app"]` to `["python", "/app/entrypoint_wrapper.py"]`. Everything else (opencode install, non-root `praf` user, `HEALTHCHECK`, `EXPOSE 8004`) is copied unchanged from upstream's Dockerfile.

A comment at the top attributes this as adapted from `pr-af`'s own `Dockerfile` (Apache-2.0) and links back to it, consistent with how the rest of this repo mirrors upstream's shape.

### 2. New `docker/entrypoint_wrapper.py`

```python
"""Runtime override for pr-af's OpenRouter-hardcoded .ai() config.

Avoids patching pr-af's source: agentfield.Agent stores ai_config as a
plain mutable attribute, read fresh on every .ai() call (not baked into a
closure at construction — confirmed against the installed agentfield
package), so it's safe to reassign here, after import and before the
server starts serving requests. No-op (falls through to pr-af's original
OpenRouter behavior) when ANTHROPIC_API_KEY isn't set.
"""

import os

import pr_af.app as _app
from agentfield import AIConfig

if os.getenv("ANTHROPIC_API_KEY"):
    _app.app.ai_config = AIConfig(
        model=os.getenv("PR_AF_AI_MODEL", "anthropic/claude-sonnet-4-5"),
        api_key=os.getenv("ANTHROPIC_API_KEY"),
        # api_base intentionally left unset — litellm's own "anthropic/<model>"
        # routing takes over and talks to Anthropic directly.
    )

_app.main()
```

### 3. `docker-compose.yml` changes

- `pr-af` service's `build.context` changes from the remote `https://github.com/Agent-Field/pr-af.git#<sha>` URL to `.` (our new local `Dockerfile`).
- Add `PR_AF_AI_MODEL=${PR_AF_AI_MODEL:-anthropic/claude-sonnet-4-5}` — needed because `PR_AF_MODEL` (used for the `.harness()` config) must stay a **bare** model id (`claude-sonnet-4-5`, no prefix — `claude_agent_sdk` takes a bare Anthropic model id, confirmed by reading `agentfield/harness/providers/claude.py`), while the `.ai()` model needs the `anthropic/` litellm prefix to route correctly. These are genuinely different values for two different subsystems, not the same env var doing double duty.
- Update the comment block above these env vars: no longer "OpenRouter regardless of PR_AF_PROVIDER" — reflect that `.ai()` now follows `ANTHROPIC_API_KEY` too, falls back to OpenRouter only when it's unset.

### 4. Small supporting fixes (bundled in, same theme)

- `src/pr_af_gitlab/config.py`: update the `CI_JOB_TOKEN` comment from "verify before relying on it" to the now-confirmed fact (GitLab's own docs: job tokens get read-only Notes API access, never `POST`) — it cannot post discussions, full stop; the fallback stays in code only because it's still valid for `fetch_mr`'s read-only call.
- `src/pr_af_gitlab/gitlab/client.py`: add a short comment on `_build_position` noting the one GitLab position-mapping edge case not handled — a comment on a genuinely *unchanged* context line wants both `old_line` and `new_line` set; `pr-af` findings essentially never target unchanged lines, so this is a documented limitation, not a redesign.
- `README.md`: reflect the new `Dockerfile`/`docker/` files, the corrected `CI_JOB_TOKEN` guidance, and the new `PR_AF_AI_MODEL` variable in the config table.
- Root `PLAN.md`: add a short amendment note near §3 (integration strategy) and §7.2/§12.7 (OpenRouter) pointing at this step's folder for the revision — the original text stays intact, per the `.claude/README.md` convention of not rewriting history.

### 5. `.claude/` documentation for this step

- `.claude/steps/002-dockerfile-claude-code-and-anthropic-routing/PLAN.md` — this plan.
- `.claude/steps/002-dockerfile-claude-code-and-anthropic-routing/EXECUTION_SUMMARY.md` — written after implementation and the verification below actually run.
- Append a row to `.claude/README.md`'s index table.

## Verification (Docker is available locally — actually run this, not just described)

1. `docker build -t pr-af-gitlab-image:test .` — confirms the tarball fetch, the `agentfield[harness-claude]` install, and the wrapper copy all succeed.
2. `docker run --rm --entrypoint python pr-af-gitlab-image:test -c "import claude_agent_sdk; print('claude_agent_sdk OK')"` — confirms the actual fix for problem #1.
3. `docker run --rm -e ANTHROPIC_API_KEY=dummy --entrypoint python pr-af-gitlab-image:test -c "import os; os.environ.setdefault('ANTHROPIC_API_KEY','dummy'); import pr_af.app as a; from agentfield import AIConfig; a.app.ai_config = AIConfig(model='anthropic/claude-sonnet-4-5', api_key='dummy'); print(a.app.ai_config.api_base, a.app.ai_config.model)"` — confirms the override logic actually produces `api_base=None`, `model='anthropic/claude-sonnet-4-5'` inside the built image, without needing a live Anthropic key or a real review run.
4. `docker compose config` — confirms the updated `docker-compose.yml` still parses/merges cleanly.
5. Existing `pytest tests/` + `ruff check` — confirm the small `config.py`/`client.py` comment-only edits didn't break anything (they're comment-only, so this is a quick sanity check, not expected to change behavior).

Not covered here (same as before): an actual live review run against a real GitLab MR — still needs PLAN.md §10's manual end-to-end test plan, real API keys, and a real instance.
