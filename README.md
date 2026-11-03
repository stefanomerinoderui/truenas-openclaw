# truenas-openclaw

Docker image and Compose stack that runs the [OpenClaw](https://docs.openclaw.ai) gateway on Alpine Linux, with [Claude Code](https://docs.claude.com/en/docs/claude-code) (`claude`) and the [Antigravity CLI](https://antigravity.google) (`agy`) available in the same container. Designed to run as a TrueNAS custom app, but works on any Docker host.

**Installing on TrueNAS:** see [TRUENAS.md](TRUENAS.md) — an interactive generator produces the YAML for *Apps > Discover > Install via YAML*.

## How it works

- The image (`node:24-alpine`) only contains Node.js, pnpm (via corepack) and system dependencies. **No node packages are baked into the image.**
- Global node packages (`openclaw`, `@anthropic-ai/claude-code`) are installed with `pnpm add -g` into the `tools` volume, together with the pnpm store and the `agy` binary. They survive image rebuilds and container recreation.
- `docker compose up` runs three stages in order:

  | Stage | Service | What it does |
  |---|---|---|
  | 1 | `update` | Installs missing packages, runs `pnpm update -g --latest`, approves their build scripts, installs `agy` if missing. Exits. |
  | 2 | `doctor` | On first run, seeds the gateway config (`gateway.mode=local`, `gateway.bind=lan`, Control UI origins). Runs `openclaw doctor --fix --non-interactive`. Exits. |
  | 3 | `openclaw` | Starts the gateway on port `18789`. |

  Each stage only starts if the previous one exited successfully, so a failed install never starts a broken gateway.

## Files

| File | Purpose |
|---|---|
| `Dockerfile` | Alpine image with Node 24, pnpm, `tini`, `tzdata`, Claude Code runtime deps (`libgcc`, `libstdc++`, `ripgrep`) and `gcompat`. |
| `docker-entrypoint.sh` | Stage logic: `update`, `doctor`, `setup` (both), or any command (runs `setup` first unless `RUN_SETUP=0`). |
| `docker-compose.yml` | The staged stack and its volumes. |
| `truenas/` | Full-screen terminal editor (Textual) that generates the TrueNAS *Install via YAML* definition and saves/edits a JSON config, with secrets kept in `.env` (see [TRUENAS.md](TRUENAS.md#1-generate-the-yaml)). |
| `TRUENAS.md` | TrueNAS installation guide. |
| `.github/workflows/docker-publish.yml` | CI: lint + tests, then builds and pushes the image to GHCR (see [TRUENAS.md](TRUENAS.md#from-github-actions-ghcr)). |
| `.env.example` | Template for `.env`. |

## Requirements

- Docker Engine with Compose v2.24+ (for optional `env_file`).
- Outbound internet access on start (npm registry and `antigravity.google`).

## Setup

1. Create the environment file and generate a gateway token:

   ```sh
   cp .env.example .env
   openssl rand -hex 32   # paste into OPENCLAW_GATEWAY_TOKEN
   ```

2. Add the URL you will open the Control UI from to `OPENCLAW_CONTROL_UI_ORIGINS`, e.g.
   `["http://truenas.local:18789","http://localhost:18789"]`.

3. Build and start:

   ```sh
   docker compose up -d --build
   ```

   The first start downloads all packages (a few hundred MB) and can take several minutes. Follow it with:

   ```sh
   docker compose logs -f update doctor openclaw
   ```

4. Check the gateway:

   ```sh
   curl -fsS http://127.0.0.1:18789/healthz
   docker inspect -f '{{.State.Health.Status}}' openclaw
   ```

5. Open `http://<host>:18789/` and authenticate with the gateway token.

### Logging in to the CLIs

Credentials are stored in the `claude` and `gemini` volumes, so this is a one-time step.

```sh
docker exec -it openclaw claude   # then follow the /login flow
docker exec -it openclaw agy      # headless OAuth: URL + code
```

Alternatively, set `ANTHROPIC_API_KEY` and/or `ANTIGRAVITY_API_KEY` in `.env` for non-interactive auth.

## Configuration

All settings go in `.env`.

| Variable | Default | Description |
|---|---|---|
| `OPENCLAW_GATEWAY_TOKEN` | — | **Required.** Gateway / Control UI token. Without it (or `OPENCLAW_GATEWAY_PASSWORD`, or `gateway.auth` in the config) the gateway refuses to bind to the LAN. |
| `OPENCLAW_CONTROL_UI_ORIGINS` | `["http://localhost:18789","http://127.0.0.1:18789"]` | JSON array of allowed Control UI origins. **Only applied on first start**; later change it with `openclaw config set` (see below). |
| `ANTHROPIC_API_KEY` | — | Optional API key for Claude Code. |
| `ANTIGRAVITY_API_KEY` | — | Optional API key for `agy`. |
| `PNPM_GLOBAL_PACKAGES` | `openclaw @anthropic-ai/claude-code` | Space-separated global packages to install/update. Add packages here to have them installed on the tools volume. |
| `SKIP_UPDATE` | `0` | `1` skips `pnpm update` (missing packages are still installed). Useful to pin current versions after a bad upstream release. |
| `RUN_SETUP` | `1` | Set to `0` by Compose for the gateway service, since the stages already ran. Only relevant when running the image without Compose. |

Changing OpenClaw settings after the first start:

```sh
docker exec openclaw openclaw config set gateway.controlUi.allowedOrigins '["http://truenas.local:18789"]'
docker compose restart openclaw
```

## Volumes

| Volume | Mount | Contents |
|---|---|---|
| `tools` | `/home/node/.local` | pnpm global packages (`share/pnpm`), pnpm store, `agy` binary |
| `openclaw` | `/home/node/.openclaw` | OpenClaw config and state |
| `claude` | `/home/node/.claude` | Claude Code credentials and settings |
| `gemini` | `/home/node/.gemini` | Antigravity CLI credentials and settings |

Removing the `tools` volume forces a clean reinstall on the next `docker compose up`; the other volumes hold your configuration and credentials.

### TrueNAS host paths

To store data on a dataset instead of named volumes, replace the entries under `x-openclaw-base.volumes` in `docker-compose.yml`:

```yaml
volumes:
  - /mnt/pool/apps/openclaw/tools:/home/node/.local
  - /mnt/pool/apps/openclaw/openclaw:/home/node/.openclaw
  - /mnt/pool/apps/openclaw/claude:/home/node/.claude
  - /mnt/pool/apps/openclaw/gemini:/home/node/.gemini
```

The container runs as `node` (uid/gid 1000), so the directories must be owned by that user. This needs root because the dataset is outside your home directory:

```sh
sudo mkdir -p /mnt/pool/apps/openclaw/{tools,openclaw,claude,gemini}
sudo chown -R 1000:1000 /mnt/pool/apps/openclaw
```

If ownership is wrong, the `update` stage stops with a `not writable by uid 1000` error.

## Updating

Packages are updated every time the stack is brought up:

```sh
docker compose up -d
```

Notes:

- A plain container restart (crash, host reboot, `docker compose restart openclaw`) restarts **only the gateway** and does not update. Run `docker compose up -d` to go through the stages again.
- `agy` is not an npm package; it updates itself in the background while in use, so the `update` stage only installs it when missing.
- To update the base image (Node, pnpm, Alpine packages): `docker compose build --pull && docker compose up -d`.

### One-off tools

Use `pnpx` to run a package without adding it to the global set:

```sh
docker exec -it openclaw pnpx <package> [args]
```

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| Gateway exits with `Refusing to bind gateway to lan without auth` | Set `OPENCLAW_GATEWAY_TOKEN` in `.env`, then `docker compose up -d`. |
| `Gateway start blocked: existing config is missing gateway.mode` | The config was reset or edited. `docker exec openclaw openclaw config set gateway.mode local`, or run the `doctor` stage again with `docker compose up -d`. |
| Control UI rejects the origin | Add your URL to `gateway.controlUi.allowedOrigins` (see Configuration). |
| `update` stage fails on first start | No network access, or volume not writable. Check `docker compose logs update`. |
| `ERROR: logging before google.Init` lines in the `update` logs | Harmless log noise from the `agy` installer. |
| `Ignored build scripts` in the `update` logs | Expected: pnpm blocks build scripts by default; the stage approves and runs them right after (`pnpm approve-builds -g --all`). |
# truenas-openclaw
