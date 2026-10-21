FROM node:24-bookworm-slim@sha256:a5f4366e834695d5f363b920c104953a535d3a9864d1bdf74566a0ebe01daee5
SHELL ["/bin/bash", "-o", "pipefail", "-c"]
ARG CODEX_VERSION=0.163.0
ARG CLAUDE_VERSION=2.1.306
ARG BUN_VERSION=1.4.3
ARG UV_VERSION=0.13.4
ARG RTK_VERSION=0.41.0
ARG RTK_SHA256=90ae10f5c76de9bacaec5eeeefb6012f74dd47f4e280ec614295555b64da6b57
RUN apt-get update && apt-get install -y --no-install-recommends \
    bash ca-certificates curl git gh ripgrep fd-find jq less unzip \
    python3 python3-venv python3-pip build-essential pkg-config openssh-client \
    procps util-linux sudo && rm -rf /var/lib/apt/lists/*
RUN npm install -g @openai/codex@${CODEX_VERSION} @anthropic-ai/claude-code@${CLAUDE_VERSION} bun@${BUN_VERSION} \
    && python3 -m venv /opt/uv && /opt/uv/bin/pip install --no-cache-dir uv==${UV_VERSION} \
    && ln -s /opt/uv/bin/uv /usr/local/bin/uv \
    && ln -s /opt/uv/bin/uvx /usr/local/bin/uvx \
    && curl -fsSL "https://github.com/rtk-ai/rtk/releases/download/v${RTK_VERSION}/rtk-x86_64-unknown-linux-musl.tar.gz" -o /tmp/rtk.tar.gz \
    && echo "${RTK_SHA256}  /tmp/rtk.tar.gz" | sha256sum -c - \
    && tar -xzf /tmp/rtk.tar.gz -C /usr/local/bin rtk && rm /tmp/rtk.tar.gz \
    && ln -s /usr/bin/fdfind /usr/local/bin/fd
COPY skills/ /opt/dev/skills/
COPY instructions.md /opt/dev/instructions.md
COPY entrypoint.sh agent-session.sh dev-finish.sh gh.sh /usr/local/lib/dev/
RUN chmod +x /usr/local/lib/dev/*.sh \
    && ln -s /usr/local/lib/dev/dev-finish.sh /usr/local/bin/dev-finish \
    && ln -s /usr/local/lib/dev/agent-session.sh /usr/local/bin/dev-agent \
    && ln -s /usr/local/lib/dev/gh.sh /usr/local/bin/gh \
    && echo 'node ALL=(root) NOPASSWD: ALL' > /etc/sudoers.d/dev \
    && chmod 440 /etc/sudoers.d/dev
ENV HOME=/home/node CODEX_HOME=/agent/codex CLAUDE_CONFIG_DIR=/agent/claude \
    DISABLE_AUTOUPDATER=1 DEV_CONTAINER=1 PONYTAIL_DEFAULT_MODE=full \
    PATH=/home/node/.local/bin:/home/node/.bun/bin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
WORKDIR /workspace
ENTRYPOINT ["/usr/local/lib/dev/entrypoint.sh"]
CMD ["sleep", "infinity"]
