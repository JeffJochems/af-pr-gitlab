# `.claude/` — plan & execution history

This folder is the durable record of every substantive change made to this repo through Claude Code: one numbered **step** per unit of work, each with the plan that was reviewed/approved *before* implementation and a summary of what actually happened *after*.

## Structure

```
.claude/
├── README.md                      # this file — the convention itself
└── steps/
    ├── 001-gitlab-adapter-scaffold/
    │   ├── PLAN.md
    │   └── EXECUTION_SUMMARY.md
    └── 002-<next-slug>/
        ├── PLAN.md
        └── EXECUTION_SUMMARY.md
```

## The rule

**Every step needs both files, in its own numbered folder, before it's considered done:**

1. **`PLAN.md`** — written and reviewed *before* implementation starts. What's being built/changed, why, the approach, and any open decisions surfaced for the user rather than silently picked. If the change is small enough that a full plan feels like overkill, it still gets a short one — a few paragraphs is fine, but it still needs its own step folder.
2. **`EXECUTION_SUMMARY.md`** — written *after* implementation, once it's actually been verified (tests run, lint clean, etc. — whatever verification the change allows). Records:
   - What was actually built, as a table or list of paths.
   - What verification was actually performed (and, just as importantly, what verification was *not* possible and why — e.g. "requires a live instance this pass didn't have").
   - Anything the plan got wrong, or any bug found only during implementation — call these out explicitly rather than folding them in silently. If the plan and the as-built reality match perfectly, say so; don't manufacture discrepancies, but don't paper over real ones either.
   - What's still open / unresolved, carried over from the plan's open decisions if they weren't settled by this step.

Do not backfill or rewrite an earlier step's files once a later step has started — each folder is a point-in-time record. If a later step changes a decision an earlier plan made, say so in the later step's own files; don't edit history.

## Numbering

Steps are numbered sequentially, zero-padded to 3 digits (`001`, `002`, ... `010`, ...), each with a short kebab-case slug describing the work (`001-gitlab-adapter-scaffold`, not `001-work` or `001-updates`). Bump the number for each new unit of work — a new feature, a significant refactor, a design pivot — not for every individual file edit within one already-planned unit of work (those belong in the same step's `EXECUTION_SUMMARY.md`).

## Index

| Step | Summary |
|---|---|
| [001-gitlab-adapter-scaffold](steps/001-gitlab-adapter-scaffold/) | Initial plan for the GitLab adapter around `pr-af`, then its implementation: `GitLabClient`, schemas, config, CI templates, docker-compose, tests. |
| [002-dockerfile-claude-code-and-anthropic-routing](steps/002-dockerfile-claude-code-and-anthropic-routing/) | Fixes two bugs found via live research (real `agentfield` package + GitLab 17.7 docs): `claude-code` provider needs a dependency upstream's image doesn't install; `.ai()`'s OpenRouter hardcoding is fixed via a runtime override, not a source patch. Revises step 001's "zero Dockerfile" integration strategy. |

Append a row here for every new step — this table is the fast way to find which folder covers what, without opening each one.
