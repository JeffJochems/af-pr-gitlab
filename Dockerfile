# Adapted from pr-af's own Dockerfile (Apache-2.0):
# https://github.com/Agent-Field/pr-af/blob/main/Dockerfile
#
# Mirrors both of its stages almost line-for-line. Three deliberate deltas,
# each justified in .claude/steps/002-dockerfile-claude-code-and-anthropic-routing/PLAN.md:
#   1. Source is fetched from the pinned upstream commit's tarball instead of
#      COPY'd from a local build context (we don't vendor pr-af's source).
#   2. The builder stage installs "agentfield[harness-claude]" instead of
#      plain "agentfield" — PR_AF_PROVIDER=claude-code needs claude_agent_sdk,
#      which upstream's own Dockerfile does not install.
#   3. The runtime stage's CMD runs docker/entrypoint_wrapper.py instead of
#      `python -m pr_af.app` directly — a runtime override for pr-af's
#      OpenRouter-hardcoded .ai() config, applied without patching pr-af's
#      source at all (see the wrapper's own docstring).
#
# Keep PR_AF_PINNED_REF (the commit SHA below) in sync with docker-compose.yml's
# comment and pyproject.toml's `pr-af @ git+...` pin.

FROM python:3.11-slim AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    git \
    curl && \
    rm -rf /var/lib/apt/lists/*

# Delta 1: fetch the pinned commit's source instead of using it as the build
# context. GitHub's archive endpoint is a stable, official way to fetch a
# repo snapshot at any commit SHA.
ARG PR_AF_PINNED_REF=6b82efc8ade7cd48420ecd6de59eeb1cb80d3b49
ADD https://github.com/Agent-Field/pr-af/archive/${PR_AF_PINNED_REF}.tar.gz /tmp/pr-af.tar.gz
RUN mkdir -p /tmp/pr-af-src && \
    tar -xzf /tmp/pr-af.tar.gz --strip-components=1 -C /tmp/pr-af-src && \
    rm /tmp/pr-af.tar.gz

# Delta 2: agentfield[harness-claude] instead of plain agentfield — everything
# else in this list is identical to upstream's own Dockerfile.
RUN pip install --no-cache-dir --prefix=/install \
    "agentfield[harness-claude]>=0.1.84" \
    "hax-sdk>=0.2.4" \
    "pydantic>=2.0" \
    "httpx>=0.27" \
    "python-dotenv>=1.0" \
    "fastapi>=0.100" \
    "uvicorn>=0.20" \
    "PyJWT[crypto]>=2.8" && \
    pip install --no-cache-dir --prefix=/install --no-deps /tmp/pr-af-src


FROM python:3.11-slim AS runtime

ARG OPENCODE_VERSION=1.17.15

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    AGENTFIELD_SERVER=http://agentfield:8080 \
    PR_AF_PROVIDER=opencode \
    PR_AF_MODEL=openrouter/moonshotai/kimi-k2.5 \
    PORT=8004 \
    HOME=/home/praf \
    PYTHONPATH=/app/src \
    PATH=/home/praf/.opencode/bin:${PATH} \
    XDG_DATA_HOME=/home/praf/.local/share \
    PR_AF_WORKDIR=/workspaces

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    curl \
    git && \
    groupadd --gid 10001 praf && \
    useradd --uid 10001 --gid praf --create-home --home-dir /home/praf --shell /bin/sh praf && \
    su -s /bin/sh praf -c "curl -fsSL https://opencode.ai/install | bash -s -- --version ${OPENCODE_VERSION} --no-modify-path" && \
    mkdir -p /workspaces /home/praf/.local/share && \
    chown -R praf:praf /app /workspaces /home/praf && \
    rm -rf /var/lib/apt/lists/*

COPY --from=builder /install /usr/local
COPY --from=builder /tmp/pr-af-src/src/ /app/src/
COPY --from=builder /tmp/pr-af-src/docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN chmod +x /usr/local/bin/docker-entrypoint.sh

# Delta 3: our runtime override, not pr-af's own entrypoint module.
COPY docker/entrypoint_wrapper.py /app/entrypoint_wrapper.py

USER praf

EXPOSE 8004

HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
    CMD curl -f http://localhost:8004/health || exit 1

ENTRYPOINT ["/usr/local/bin/docker-entrypoint.sh"]
CMD ["python", "/app/entrypoint_wrapper.py"]
