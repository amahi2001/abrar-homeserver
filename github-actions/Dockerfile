ARG RUNNER_BASE=ghcr.io/actions/actions-runner:2.338.1@sha256:e275409999c37162649aa050ca573f03eab2eabcb3a1e7fcc47548de6b4f9690
FROM ${RUNNER_BASE}
USER root
RUN apt-get update && apt-get upgrade -y && apt-get install -y --no-install-recommends \
    build-essential ca-certificates gh pkg-config python3 python-is-python3 xz-utils zstd \
    && rm -rf /var/lib/apt/lists/*
USER runner
RUN curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs -o /tmp/rustup-init.sh \
    && sh /tmp/rustup-init.sh -y --profile minimal --default-toolchain none --no-modify-path \
    && rm /tmp/rustup-init.sh
ENV PATH="/home/runner/.cargo/bin:${PATH}" \
    FORCE_JAVASCRIPT_ACTIONS_TO_NODE24=true \
    ACTIONS_RUNNER_FORCED_INTERNAL_NODE_VERSION=node24 \
    ACTIONS_ALLOW_USE_UNSECURE_NODE_VERSION=false
