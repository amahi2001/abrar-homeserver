FROM ghcr.io/actions/actions-runner:2.338.0@sha256:4ffadc0002b2581327e06101fc8c06cd189232baf79fe561fac9caeb76f5e807
USER root
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential ca-certificates gh pkg-config python3 python-is-python3 xz-utils \
    && rm -rf /var/lib/apt/lists/*
USER runner
RUN curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs -o /tmp/rustup-init.sh \
    && sh /tmp/rustup-init.sh -y --profile minimal --default-toolchain none --no-modify-path \
    && rm /tmp/rustup-init.sh
ENV PATH="/home/runner/.cargo/bin:${PATH}"
