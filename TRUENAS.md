# Installing on TrueNAS via YAML

This guide deploys the OpenClaw stack as a TrueNAS custom app using **Apps > Discover > ⋮ (top-right menu) > Install via YAML**. The YAML is produced by an interactive generator, so no files have to be copied to the NAS.

## Prerequisites

- TrueNAS SCALE / Community Edition **24.10 (Electric Eel) or later** (Docker-based apps), with an apps pool selected in **Apps > Configuration > Choose Pool**.
- On the machine where you run the generator: a checkout of this repository and [`uv`](https://docs.astral.sh/uv/) (it installs the Python dependencies automatically). Python 3.11+.
- Outbound internet access from TrueNAS (Docker Hub, npm registry, `antigravity.google`).

Optional but recommended: create a dataset for the app data, e.g. `tank/apps/openclaw` (**Datasets > Add Dataset**, preset *Apps*). Ownership is fixed automatically on first start.

## 1. Generate the YAML

From the repository root:

```sh
uv run truenas/generate_yaml.py            # writes openclaw-truenas.yaml
uv run truenas/generate_yaml.py -o my.yaml # custom output path
```

The TUI walks through these sections (arrow keys to select, Enter to confirm, Ctrl-C to abort):

| Section | Prompt | Notes |
|---|---|---|
| Image | Build from embedded Dockerfile / prebuilt image | **Build** (default) embeds the `Dockerfile` and `docker-entrypoint.sh` into the YAML; TrueNAS builds the image itself. **Prebuilt** pulls an image you pushed to a registry (see [Prebuilt image](#prebuilt-image)). |
| Network | Bridge / host | **Bridge** publishes the gateway port; **host** uses the TrueNAS network stack directly (no port mapping). |
| | Gateway port | Port on TrueNAS, default `18789`. Avoid ports already used by the TrueNAS UI (80/443) or other apps. |
| | Bind address | Bridge only. `0.0.0.0` = all interfaces; set a specific IP to expose the gateway on one interface only. |
| | TrueNAS hostnames/IPs | Used to build the Control UI allowed origins (`http://<host>:<port>`); `localhost` is always added. |
| | Extra origins | Optional full URLs, e.g. a reverse proxy `https://openclaw.example.com`. |
| Storage | Host paths / named volumes | **Host paths** creates `tools/`, `openclaw/`, `claude/`, `gemini/` under the base path (e.g. `/mnt/tank/apps/openclaw`) and adds a `permissions` stage that `chown`s them to uid 1000. **Named volumes** are managed by Docker. |
| Environment | Gateway token | Random 64-hex token by default; shown after writing so you can save it. |
| | `ANTHROPIC_API_KEY`, `ANTIGRAVITY_API_KEY` | Optional; leave empty to log in interactively later. |
| | Timezone | Defaults to this machine's timezone. |
| | Global pnpm packages | Default `openclaw @anthropic-ai/claude-code`. |
| | Skip package updates | `no` = update on every start (only missing packages are installed when `yes`). |

After the summary it writes the YAML with mode `600` and can print the gateway token and the YAML.

> **The YAML contains the gateway token and any API keys.** Don't commit or share it, and delete the local file once the app is installed.

## 2. Install in TrueNAS

1. Open **Apps** and click **Discover Apps**.
2. Click the **⋮** menu at the top right and choose **Install via YAML**.
3. Enter an app **Name** (lowercase, e.g. `openclaw`).
4. Paste the whole generated YAML into the editor.
5. Click **Save**.

TrueNAS then runs the stages in order:

| Stage | Runs when | What it does |
|---|---|---|
| `permissions` | Host paths only | As root: `chown`s the data directories to `1000:1000` if they are not owned by uid 1000 yet. Builds the image in build mode. |
| `update` | Always | Installs/updates global pnpm packages and installs `agy` if missing. Builds the image in build mode with named volumes. |
| `doctor` | After `update` succeeds | Seeds the initial gateway config, runs `openclaw doctor --fix`. |
| `openclaw` | After `doctor` succeeds | The gateway. |

The first deploy builds the image and downloads several hundred MB, so it can take several minutes. Follow progress in **Apps > Installed > *openclaw* > Workloads** (logs icon per container). The app shows as running once the `openclaw` container is healthy.

## 3. First use

1. Open `http://<truenas-host>:<port>/` (one of the origins you entered) and log in with the gateway token.
2. Log in to the CLIs once (credentials are persisted). Open a shell in the `openclaw` container from **Workloads** (shell icon), or from the TrueNAS shell:

   ```sh
   sudo docker ps --format '{{.Names}}' | grep openclaw   # find the gateway container name
   sudo docker exec -it <container> claude                  # follow /login
   sudo docker exec -it <container> agy                     # headless OAuth: URL + code
   ```

   `sudo` is needed because the Docker socket on TrueNAS is root-only.

## Updating

- **Packages** (`openclaw`, `claude`, …) update whenever the stack is redeployed: **Apps > Installed > *openclaw* > Edit > Save**, or Stop then Start. A container restart after a crash or reboot only restarts the gateway.
- **`agy`** updates itself while in use.
- **Image changes** (this repository's `Dockerfile`/`docker-entrypoint.sh` changed): run the generator again with the same answers, then **Edit**, paste the new YAML and **Save**. Keep the same gateway token (choose *no* at “Generate a random gateway token?” and paste the old one).
- **Changing settings** such as origins or port: regenerate and paste, or edit the values directly in the app's YAML. `OPENCLAW_CONTROL_UI_ORIGINS` is only applied on the very first start; afterwards change it inside the container:

  ```sh
  sudo docker exec <container> openclaw config set gateway.controlUi.allowedOrigins '["http://truenas.local:18789"]'
  ```

## Prebuilt image

Use this if the build mode doesn't work on your TrueNAS version, or to avoid building on the NAS.

### From GitHub Actions (GHCR)

When this repository is on GitHub, `.github/workflows/docker-publish.yml` builds the image for `linux/amd64` and `linux/arm64` and pushes it to the GitHub Container Registry:

| Trigger | Tags pushed |
|---|---|
| Push to `main` | `latest`, `sha-<short>` |
| Tag `v1.2.3` | `1.2.3`, `1.2`, `sha-<short>` |
| Every Monday 04:00 UTC | `latest` (rebuild with the newest Alpine base) |
| Pull request | nothing (build check only) |
| Manual (*Actions > Build and publish image > Run workflow*) | as for the selected branch/tag |

The image is `ghcr.io/<owner>/<repo>:latest` (lowercase). Lint and tests must pass before the build runs, and pushed images get a signed build provenance attestation (`gh attestation verify oci://ghcr.io/<owner>/<repo>:latest --owner <owner>`).

GHCR packages are **private** by default. Either make the package public (*GitHub > your profile > Packages > the package > Package settings > Change visibility*), or add a GHCR login in TrueNAS under **Apps > Configuration > Manage Container Image Registries** (username = GitHub user, password = a personal access token with `read:packages`).

### Manually

From the repository root, on a machine logged in to your registry (`docker login ghcr.io`):

```sh
docker build -t ghcr.io/<user>/truenas-openclaw:latest .
docker push ghcr.io/<user>/truenas-openclaw:latest
```

### Using it

Choose **Pull a prebuilt image** in the generator and enter the image reference. In this mode the `update` stage pulls the image on every deploy, so redeploying the app picks up new `latest` builds.

## Troubleshooting

| Symptom | Fix |
|---|---|
| Save fails with an error about `configs`, `content` or `dockerfile_inline` | The build mode needs Docker Compose ≥ 2.23.1 on TrueNAS (`sudo docker compose version`). Use the [prebuilt image](#prebuilt-image) mode. |
| `update` stage exits with `not writable by uid 1000` | Named volumes: shouldn't happen; host paths: check the dataset ACL allows uid 1000 (the `permissions` stage only changes Unix ownership), or use an *Apps* preset dataset. |
| Gateway restarts with `Refusing to bind gateway to lan without auth` | `OPENCLAW_GATEWAY_TOKEN` is empty in the YAML. Edit the app and set it. |
| Control UI says the origin is not allowed | The URL in the browser isn't in the allowed origins. Add it (see Updating). |
| Port conflict on deploy | Choose another gateway port in the generator. |
| `Ignored build scripts` / `ERROR: logging before google.Init` in `update` logs | Harmless; see the main [README](README.md#troubleshooting). |

## Development

The generator lives in `truenas/`: `compose_builder.py` builds the Compose document (pure logic), `generate_yaml.py` is the TUI. Tests and lint:

```sh
cd truenas
uv run --no-project --with pytest --with pyyaml --with questionary pytest
ruff check . && ruff format --check .
```
