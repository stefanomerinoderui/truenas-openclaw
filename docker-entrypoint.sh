#!/bin/sh
# Container setup stages, selectable by the first argument:
#   update  install/update global pnpm packages and install agy if missing
#   doctor  seed the initial OpenClaw config and run `openclaw doctor --fix`
#   setup   update + doctor
# Any other command runs `setup` first (unless RUN_SETUP=0) and is then exec'd.
# docker-compose.yml runs the stages as separate one-shot services.
set -eu

PNPM_GLOBAL_PACKAGES="${PNPM_GLOBAL_PACKAGES:-openclaw @anthropic-ai/claude-code}"
SKIP_UPDATE="${SKIP_UPDATE:-0}"
RUN_SETUP="${RUN_SETUP:-1}"
AGY_INSTALL_URL="https://antigravity.google/cli/install.sh"
AGY_BIN="$HOME/.local/bin/agy"
# JSON array of origins allowed to open the Control UI (add your TrueNAS URL).
OPENCLAW_CONTROL_UI_ORIGINS="${OPENCLAW_CONTROL_UI_ORIGINS:-[\"http://localhost:18789\",\"http://127.0.0.1:18789\"]}"

log() { printf '[entrypoint] %s\n' "$*"; }
warn() { printf '[entrypoint] WARNING: %s\n' "$*" >&2; }
die() { printf '[entrypoint] ERROR: %s\n' "$*" >&2; exit 1; }

check_volume_writable() {
    mkdir -p "$PNPM_HOME" "$HOME/.local/bin" 2>/dev/null || true
    if ! touch "$PNPM_HOME/.write-test" 2>/dev/null; then
        die "$PNPM_HOME is not writable by uid $(id -u). If it is a host path, run: chown -R 1000:1000 <host-path>"
    fi
    rm -f "$PNPM_HOME/.write-test"
}

# Prints the packages from PNPM_GLOBAL_PACKAGES that are not installed globally.
missing_pnpm_packages() {
    installed_json="$(pnpm ls -g --depth 0 --json 2>/dev/null || echo '[]')"
    # SC2016: the single-quoted JS is intentionally not expanded by the shell.
    # SC2086: word splitting of the package list is intended.
    # shellcheck disable=SC2016,SC2086
    printf '%s' "$installed_json" | node -e '
        const wanted = process.argv.slice(1);
        let data = "";
        process.stdin.on("data", (chunk) => (data += chunk));
        process.stdin.on("end", () => {
            let installed = {};
            try {
                for (const entry of JSON.parse(data || "[]")) {
                    Object.assign(installed, entry.dependencies || {});
                }
            } catch (err) {
                console.error(`[entrypoint] cannot parse pnpm ls output: ${err.message}`);
            }
            const missing = wanted.filter((name) => !(name in installed));
            if (missing.length) console.log(missing.join(" "));
        });
    ' $PNPM_GLOBAL_PACKAGES
}

install_or_update_pnpm_packages() {
    missing="$(missing_pnpm_packages)"
    if [ -n "$missing" ]; then
        log "Installing global pnpm packages: $missing"
        set --
        for package in $missing; do
            set -- "$@" "$package@latest"
        done
        pnpm add -g "$@" || die "pnpm install failed for: $missing"
        approve_global_builds || die "build scripts failed for: $missing"
    fi

    if [ "$SKIP_UPDATE" = "1" ]; then
        log "SKIP_UPDATE=1, not updating global pnpm packages"
        return
    fi
    log "Updating global pnpm packages"
    pnpm update -g --latest || warn "pnpm update failed, keeping current versions"
    approve_global_builds || warn "build scripts failed after update"
}

# pnpm blocks dependency build scripts by default, but openclaw and
# claude-code need their postinstall steps (e.g. claude's native binary).
# This approves and runs every pending global build; no-op when none pending.
approve_global_builds() {
    pnpm approve-builds -g --all
}

# agy is a standalone binary, not an npm package. Its installer refuses to run
# over an existing binary because agy self-updates while in use, so only
# install it when missing.
install_agy() {
    if [ -x "$AGY_BIN" ]; then
        return
    fi
    log "Installing Antigravity CLI (agy)"
    curl -fsSL "$AGY_INSTALL_URL" | bash || die "agy install failed"
}

warn_if_gateway_auth_missing() {
    if [ -z "${OPENCLAW_GATEWAY_TOKEN:-}" ] && [ -z "${OPENCLAW_GATEWAY_PASSWORD:-}" ]; then
        warn "OPENCLAW_GATEWAY_TOKEN is not set; the gateway refuses to bind to lan without auth unless gateway.auth is configured"
    fi
}

# A fresh state volume has no gateway.mode, and the gateway refuses to start
# without it. Seed the same settings the official Docker setup writes.
ensure_openclaw_config() {
    if [ -n "$(openclaw config get gateway.mode 2>/dev/null)" ]; then
        return
    fi
    log "Seeding initial OpenClaw gateway config"
    openclaw config set --batch-json "[
        {\"path\": \"gateway.mode\", \"value\": \"local\"},
        {\"path\": \"gateway.bind\", \"value\": \"lan\"},
        {\"path\": \"gateway.controlUi.allowedOrigins\", \"value\": $OPENCLAW_CONTROL_UI_ORIGINS}
    ]" || die "could not write initial OpenClaw config"
}

run_openclaw_doctor() {
    log "Running openclaw doctor"
    openclaw doctor --fix --non-interactive || warn "openclaw doctor reported problems"
}

stage_update() {
    check_volume_writable
    install_or_update_pnpm_packages
    install_agy
}

stage_doctor() {
    command -v openclaw >/dev/null || die "openclaw is not installed; run the update stage first"
    ensure_openclaw_config
    run_openclaw_doctor
}

case "${1:-}" in
    update) stage_update ;;
    doctor) stage_doctor ;;
    setup)
        stage_update
        stage_doctor
        ;;
    *)
        if [ "$RUN_SETUP" != "0" ]; then
            stage_update
            stage_doctor
        fi
        warn_if_gateway_auth_missing
        exec "$@"
        ;;
esac
