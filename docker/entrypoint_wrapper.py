"""Runtime override for pr-af's OpenRouter-hardcoded .ai() config.

pr-af's own app.py constructs its Agent with
``AIConfig(api_base="https://openrouter.ai/api/v1", ...)`` unconditionally,
which routes every ``.ai()`` call (the comment-polish and merge-blocking-gate
passes) through OpenRouter regardless of the harness provider setting.

This avoids patching pr-af's source to fix that: ``agentfield.Agent`` stores
``ai_config`` as a plain mutable attribute, read fresh on every ``.ai()`` call
(confirmed against the installed ``agentfield`` package — not baked into a
closure at construction), so it's safe to reassign here, after import and
before the server starts serving requests.

No-op (falls through to pr-af's original OpenRouter behavior) when
ANTHROPIC_API_KEY isn't set, so this is fully backward compatible with
deployments that haven't set it.
"""

import os

import pr_af.app as _app
from agentfield import AIConfig

if os.getenv("ANTHROPIC_API_KEY"):
    _app.app.ai_config = AIConfig(
        model=os.getenv("PR_AF_AI_MODEL", "anthropic/claude-sonnet-4-5"),
        api_key=os.getenv("ANTHROPIC_API_KEY"),
        # api_base intentionally left unset: litellm's own "anthropic/<model>"
        # routing takes over and talks to Anthropic directly, instead of
        # pr-af's own hardcoded OpenRouter endpoint.
    )

_app.main()
