# Code Studio: one image, two services (IDE and Agent Engine). See docs/DOCKER.md.
FROM node:22-slim AS node

FROM python:3.12-slim
ARG CLAUDE_CODE_VERSION=2.1.327
ARG CODEX_VERSION=0.169.0
ARG INSTALL_ANTIGRAVITY=1

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1 \
    DISABLE_AUTOUPDATER=1 \
    HOME=/home/app \
    USER=codestudio LOGNAME=codestudio

RUN apt-get update \
 && apt-get install -y --no-install-recommends bash ca-certificates curl git procps tini \
 && rm -rf /var/lib/apt/lists/*

# Node.js runtime for the Claude Code and Codex CLIs
COPY --from=node /usr/local/bin/node /usr/local/bin/node
COPY --from=node /usr/local/lib/node_modules /usr/local/lib/node_modules
RUN ln -s ../lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm \
 && ln -s ../lib/node_modules/npm/bin/npx-cli.js /usr/local/bin/npx \
 && npm install -g "@anthropic-ai/claude-code@${CLAUDE_CODE_VERSION}" "@openai/codex@${CODEX_VERSION}" \
 && npm cache clean --force

# Google Antigravity CLI: Google's installer downloads one binary and
# verifies its SHA-512 against the release manifest before installing.
RUN if [ "$INSTALL_ANTIGRAVITY" = "1" ]; then \
      tmp=$(mktemp -d) \
      && curl -fsSL https://antigravity.google/cli/install.sh -o "$tmp/install.sh" \
      && HOME="$tmp" bash "$tmp/install.sh" --dir /usr/local/bin \
      && rm -rf "$tmp" && agy --version; \
    fi

WORKDIR /app
COPY requirements.txt .
# Test-only packages (pytest, playwright) are not needed at runtime.
RUN grep -viE '^(pytest|playwright)' requirements.txt > /tmp/requirements.runtime.txt \
 && pip install -r /tmp/requirements.runtime.txt && rm /tmp/requirements.runtime.txt

COPY . .
RUN CODE_STUDIO_DATA_DIR=/tmp/build-data CODE_STUDIO_SECRET_KEY=build-only \
    CODE_STUDIO_STATIC_ROOT=/app/staticfiles python manage.py collectstatic --noinput -v0 \
 && rm -rf /tmp/build-data \
 && mkdir -p /home/app && chmod 0777 /home/app \
 && chmod +x docker/*.sh

ENV CODE_STUDIO_STATIC_ROOT=/app/staticfiles CODE_STUDIO_DEBUG=0
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["/app/docker/ide.sh"]
