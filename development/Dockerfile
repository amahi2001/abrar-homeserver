FROM node:24-bookworm-slim@sha256:a5f4366e834695d5f363b920c104953a535d3a9864d1bdf74566a0ebe01daee5
SHELL ["/bin/bash", "-o", "pipefail", "-c"]
ARG CODEX_VERSION=0.163.0
ARG CLAUDE_VERSION=2.1.306
ARG BUN_VERSION=1.4.3
ARG UV_VERSION=0.13.4
ARG SKILLS_VERSION=1.7.4
RUN apt-get update && apt-get install -y --no-install-recommends \
    bash ca-certificates curl git gh ripgrep fd-find jq less unzip \
    python3 python3-venv python3-pip build-essential pkg-config openssh-client \
    procps util-linux sudo && rm -rf /var/lib/apt/lists/*
RUN npm install -g @openai/codex@${CODEX_VERSION} @anthropic-ai/claude-code@${CLAUDE_VERSION} bun@${BUN_VERSION} skills@${SKILLS_VERSION} \
    && python3 -m venv /opt/uv && /opt/uv/bin/pip install --no-cache-dir uv==${UV_VERSION} \
    && ln -s /opt/uv/bin/uv /usr/local/bin/uv \
    && ln -s /opt/uv/bin/uvx /usr/local/bin/uvx \
    && ln -s /usr/bin/fdfind /usr/local/bin/fd
ARG RTK_VERSION=
ARG RTK_INSTALLER=https://raw.githubusercontent.com/rtk-ai/rtk/master/install.sh
ARG RTK_INSTALLER_SHA256=
RUN curl -fsSL "$RTK_INSTALLER" -o /tmp/rtk-install.sh \
    && if [ -n "$RTK_INSTALLER_SHA256" ]; then echo "$RTK_INSTALLER_SHA256  /tmp/rtk-install.sh" | sha256sum -c -; fi \
    && RTK_INSTALL_DIR=/usr/local/bin sh /tmp/rtk-install.sh \
    && rm /tmp/rtk-install.sh
ARG PLAYWRIGHT_CLI_VERSION=0.1.22
ENV PLAYWRIGHT_BROWSERS_PATH=/opt/playwright-browsers
RUN npm install -g @playwright/cli@${PLAYWRIGHT_CLI_VERSION} \
    && playwright-cli install-browser --with-deps chromium \
    && chmod -R a+rX /opt/playwright-browsers \
    && apt-get update && apt-get install -y --no-install-recommends \
       xvfb openbox dbus-x11 x11vnc novnc websockify xdotool scrot xterm x11-utils \
    && rm -rf /var/lib/apt/lists/*
ARG CODEBASE_MEMORY_VERSION=0.12.1
ARG CODEBASE_MEMORY_INSTALLER=https://raw.githubusercontent.com/DeusData/codebase-memory-mcp/a7158ac2f48ca646b4527341b76f930f2aeff87e/install.sh
ARG CODEBASE_MEMORY_INSTALLER_SHA256=13049c7cc51bc508d68b8ecb8a9fd9574ecb7c6f2c9dd5a19bf7d4c187321145
RUN curl -fsSL "$CODEBASE_MEMORY_INSTALLER" -o /tmp/cbm-install.sh \
    && echo "$CODEBASE_MEMORY_INSTALLER_SHA256  /tmp/cbm-install.sh" | sha256sum -c - \
    && CBM_DOWNLOAD_URL="https://github.com/DeusData/codebase-memory-mcp/releases/download/v${CODEBASE_MEMORY_VERSION}" \
       bash /tmp/cbm-install.sh --dir=/opt/codebase-memory-mcp --skip-config \
    && test "$(/opt/codebase-memory-mcp/codebase-memory-mcp --version)" = "codebase-memory-mcp ${CODEBASE_MEMORY_VERSION}" \
    && ln -s /opt/codebase-memory-mcp/codebase-memory-mcp /usr/local/bin/codebase-memory-mcp \
    && rm /tmp/cbm-install.sh
COPY instructions.md ui.md codebase-memory.ts /opt/dev/
COPY browser-state.cjs /opt/dev/
COPY playwright-cli.sh /opt/dev/bin/playwright-cli
COPY dev-ui.sh /usr/local/lib/dev/dev-ui.sh
COPY entrypoint.sh agent-session.sh dev-finish.sh gh.sh /usr/local/lib/dev/
RUN chmod +x /usr/local/lib/dev/*.sh \
    && chmod +x /opt/dev/bin/playwright-cli \
    && chmod +x /opt/dev/codebase-memory.ts \
    && ln -s /opt/dev/codebase-memory.ts /usr/local/bin/dev-memory \
    && echo 'export PATH="/opt/dev/bin:$PATH"' > /etc/profile.d/dev-ui.sh \
    && ln -s /usr/local/lib/dev/dev-finish.sh /usr/local/bin/dev-finish \
    && ln -s /usr/local/lib/dev/agent-session.sh /usr/local/bin/dev-agent \
    && ln -s /usr/local/lib/dev/dev-ui.sh /usr/local/bin/dev-ui \
    && ln -s /usr/local/lib/dev/gh.sh /usr/local/bin/gh \
    && echo 'node ALL=(root) NOPASSWD: ALL' > /etc/sudoers.d/dev \
    && chmod 440 /etc/sudoers.d/dev
ENV HOME=/home/node CODEX_HOME=/agent/codex CLAUDE_CONFIG_DIR=/agent/claude \
    DISABLE_AUTOUPDATER=1 DEV_CONTAINER=1 PONYTAIL_DEFAULT_MODE=full \
    PATH=/opt/dev/bin:/home/node/.local/bin:/home/node/.bun/bin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
WORKDIR /workspace
ENTRYPOINT ["/usr/local/lib/dev/entrypoint.sh"]
CMD ["sleep", "infinity"]
