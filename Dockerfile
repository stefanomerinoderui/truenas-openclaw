# OpenClaw gateway + Claude Code + Antigravity CLI (agy) on Alpine.
#
# The image only ships Node, pnpm and system dependencies. Global node
# packages and the agy binary live on the /home/node/.local volume and are
# installed/updated by docker-entrypoint.sh (see its stages and docker-compose.yml).
FROM node:24-alpine

# libgcc/libstdc++/ripgrep: required by Claude Code on musl.
# gcompat: glibc shim in case a prebuilt binary (e.g. agy) is not fully static.
# bash/curl: required by the agy install script.
# tzdata: lets the TZ environment variable take effect.
# Versions are not pinned on purpose: the image tracks the latest Alpine fixes.
# hadolint ignore=DL3018
RUN apk add --no-cache \
        bash \
        ca-certificates \
        curl \
        gcompat \
        git \
        libgcc \
        libstdc++ \
        ripgrep \
        tini \
        tzdata

# Corepack cache outside the home dir so pnpm stays available for the node
# user without depending on the tools volume. `pnpm --version` runs as root to
# fetch pnpm's lazily downloaded native binary into the shared cache.
ENV COREPACK_HOME=/usr/local/share/corepack \
    COREPACK_ENABLE_DOWNLOAD_PROMPT=0
RUN corepack enable pnpm \
    && corepack prepare pnpm@latest --activate \
    && pnpm --version \
    && chmod -R a+rX "$COREPACK_HOME"

# pnpm 12 links global binaries into $PNPM_HOME/bin.
ENV PNPM_HOME=/home/node/.local/share/pnpm \
    USE_BUILTIN_RIPGREP=0
ENV PATH=$PNPM_HOME/bin:$PNPM_HOME:/home/node/.local/bin:$PATH

# Pre-create mount points owned by node so named volumes inherit the ownership.
RUN mkdir -p \
        "$PNPM_HOME" \
        /home/node/.local/bin \
        /home/node/.openclaw \
        /home/node/.claude \
        /home/node/.gemini \
    && chown -R node:node /home/node

USER node
WORKDIR /home/node

# Keep the store next to the global packages so pnpm can hardlink within the
# tools volume. The rc file lives in ~/.config, outside the volume.
RUN pnpm config set --global store-dir "$PNPM_HOME/store"

COPY --chmod=755 docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh

EXPOSE 18789
VOLUME ["/home/node/.local", "/home/node/.openclaw", "/home/node/.claude", "/home/node/.gemini"]

# Long start period: the first start installs all packages on the volume.
HEALTHCHECK --interval=30s --timeout=5s --start-period=5m --retries=3 \
    CMD ["wget", "-q", "--spider", "http://127.0.0.1:18789/healthz"]

ENTRYPOINT ["/sbin/tini", "--", "docker-entrypoint.sh"]
CMD ["openclaw", "gateway", "--bind", "lan", "--port", "18789"]
