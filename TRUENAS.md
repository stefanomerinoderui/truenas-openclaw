# Installing on TrueNAS via YAML

This guide deploys the OpenClaw stack as a TrueNAS custom app using **Apps > Discover > ⋮ (top-right menu) > Install via YAML**. The YAML is produced by a full-screen terminal generator, so no files have to be copied to the NAS.

## Prerequisites

- TrueNAS SCALE / Community Edition **24.10 (Electric Eel) or later** (Docker-based apps), with an apps pool selected in **Apps > Configuration > Choose Pool**.
- On the machine where you run the generator: a checkout of this repository and [`uv`](https://docs.astral.sh/uv/) (it installs the Python dependencies automatically). Python 3.11+.
- Outbound internet access from TrueNAS (Docker Hub, npm registry, `antigravity.google`).

Optional but recommended: create a dataset for the app data, e.g. `tank/apps/openclaw` (**Datasets > Add Dataset**, preset *Apps*). Ownership is fixed automatically on first start.

## 1. Generate the YAML

The generator is a full-screen terminal app. From the repository root:

```sh
uv run truenas/generate_yaml.py                                   # new config
uv run truenas/generate_yaml.py --config openclaw-truenas.json    # open and edit a config
uv run truenas/generate_yaml.py --env-file ~/secrets.env -o my.yaml
```

| Option | Description |
|---|---|
| `-c`, `--config FILE` | JSON config to open in the editor. A file that doesn't exist yet starts a new config that is saved there. A config with invalid values still opens, with the problems marked so you can fix them; only broken JSON is refused. |
| `--env-file FILE` | Secrets file (default `./.env` when it exists). See [Secrets](#json-config-and-env-file). |
| `-o`, `--output FILE` | YAML to write (default `openclaw-truenas.yaml`); can also be changed on the *Review & generate* page. |

```
┌ OpenClaw TrueNAS generator ──────────────────── openclaw-truenas.json * ┐
│ Image              │  Network mode                                      │
│ Network ⚠          │   (•) Reverse proxy: join its Docker network ...   │
│ Authentication     │   ( ) Bridge: publish the gateway port on TrueNAS  │
│ Storage            │   ( ) Host network: use the TrueNAS network stack  │
│ Environment        │  Public URL(s) served by the proxy                 │
│ Secrets            │   [not-a-url                                  ]    │
│ Review & generate  │   Not an origin URL (scheme://host[:port])         │
│ ^s Save config  ^g Generate YAML  esc Menu  ^q Quit                     │
└─────────────────────────────────────────────────────────────────────────┘
```

**Using it**
- The menu on the left lists the sections. Move with the arrow keys (the form on the right follows), press **Enter** to jump into a section, and **Esc** to go back to the menu. **Tab** moves between fields; the mouse works too. **Space** toggles switches and radio buttons.
- Every change is checked immediately: a problem appears under its field and the section gets a `⚠` in the menu. *Review & generate* lists all problems; *Generate* and *Save* stay disabled until they are fixed.
- Optional settings are switched on where they live, and their fields appear when you do: *Also join an existing Docker network* (bridge mode), *Delegate authentication to the reverse proxy* (Authentication), *Auto-approve new UI operator devices*, *Allow a same-host loopback proxy*. Fields that don't apply to the chosen mode are hidden and left out of the config.
- List fields (URLs, hosts, bind addresses, proxy IPs, headers, users, packages, scopes) take comma-separated values or a JSON list such as `["10.0.0.1", "172.17.0.1"]`. *Per-identity operator scopes* takes a JSON object.
- The title shows the config file and `*` when there are unsaved changes. Quitting with unsaved changes asks whether to save, discard or cancel.

| Key | Action |
|---|---|
| Ctrl+S | Save the config (asks for a file name the first time) |
| Ctrl+G | Generate the YAML |
| Esc | Back to the menu |
| Ctrl+Q | Quit |

**Sections**

| Section | Fields | Notes |
|---|---|---|
| Image | Build from embedded Dockerfile / prebuilt image, image reference | **Build** (default) embeds the `Dockerfile` and `docker-entrypoint.sh` into the YAML; TrueNAS builds the image itself. **Prebuilt** pulls an image you pushed to a registry (see [Prebuilt image](#prebuilt-image)). |
| Network | Reverse proxy / bridge / host | **Reverse proxy** (default, recommended) joins the proxy's existing Docker network and publishes **no ports**: the gateway is only reachable through the proxy, which is what exposes 80/443. **Bridge** publishes the gateway port on TrueNAS; **host** uses the TrueNAS network stack directly (no port mapping). |
| | Existing network name, hostname | Reverse proxy mode, or bridge mode with *Also join an existing Docker network*. Network to join (default `ix-nginx-proxy-manager_default`) and the hostname the container has on it (default `openclaw`). See [Reverse proxy](#reverse-proxy-nginx-proxy-manager). |
| | Public URL(s) | Reverse proxy mode. The URL(s) the proxy serves, e.g. `https://openclaw.example.com`; they become the Control UI allowed origins. |
| | Gateway port | Bridge / host. Port on TrueNAS, default `18789`. Avoid ports already used by the TrueNAS UI (80/443) or other apps. |
| | Bind addresses | Bridge only. IPs the port is published on, e.g. `192.168.1.5, 10.0.0.2` (one published port per address). `0.0.0.0` = all interfaces and can't be combined with specific IPv4 addresses (port conflict); IPv6 addresses are supported. |
| | TrueNAS hostnames/IPs, extra origins | Bridge / host. Build the Control UI allowed origins (`http://<host>:<port>`, plus `localhost`); extra origins are full URLs, e.g. a reverse proxy's. |
| Authentication | Trusted proxies | Always shown: one or more IPs (or CIDRs) of your reverse proxies (`gateway.trustedProxies`). Optional with token auth, where it lets the gateway trust their `X-Forwarded-*` headers; required for trusted-proxy auth. |
| | Trusted-proxy auth (off by default) | Let the reverse proxy authenticate users instead of a gateway token: the user header, required headers, allowed users, and the advanced options. See [Trusted-proxy authentication](#trusted-proxy-authentication). |
| Storage | Host paths / named volumes | **Host paths** creates `tools/`, `openclaw/`, `claude/`, `gemini/` under the base path (e.g. `/mnt/tank/apps/openclaw`) and adds a `permissions` stage that `chown`s them to uid 1000. **Named volumes** are managed by Docker. |
| Environment | Timezone, global pnpm packages, skip package updates | Timezone defaults to this machine's. Packages default to `openclaw @anthropic-ai/claude-code`. Without *Skip*, packages are updated on every start. |
| Secrets | Gateway token, API keys, gateway password | Pre-filled from the env file / environment, masked (*Show values* reveals them). Without a token in the env file a random one is generated; *Generate* makes a new one. The gateway password is only used with trusted-proxy auth. Never saved to the config. |
| Review & generate | Summary, output file, buttons | *Generate YAML*, *Copy YAML to clipboard*, *Save config*, *Save config as...*. |

The YAML is written with mode `600`. *Copy YAML to clipboard* uses the first backend that fits your environment and is installed:

  | Environment | Backend |
  |---|---|
  | macOS | `pbcopy` |
  | Windows | `clip` |
  | WSL | `clip.exe` |
  | Linux, Wayland | `wl-copy` (package `wl-clipboard`) |
  | Linux, X11 | `xclip`, then `xsel` |
  | Termux | `termux-clipboard-set` |
  | Anything else in a terminal, e.g. over SSH | OSC 52 escape sequence (needs a terminal that allows it; there is no confirmation that it worked) |


  When none is available, the copy goes through the terminal (OSC 52); there is no confirmation that it worked, so paste to check. The clipboard then holds the gateway token and API keys, so clear it after pasting.

> **The YAML contains the gateway token and any API keys.** Don't commit or share it, and delete the local file once the app is installed.

### JSON config and env file

The config (*Save config*, default `openclaw-truenas.json`) holds every setting except secrets, so it can be shared or committed. Open it again with `--config` to change something and regenerate. The saved file only contains keys relevant to the chosen modes; [`truenas/config.example.json`](truenas/config.example.json) is a ready-made example. Unknown keys and secrets found in a config you open are reported and dropped when you save.

**Secrets** come from a dotenv-style file (`--env-file`, default `./.env` when it exists) and from real environment variables, which win over the file. Only these variables are read; empty values count as unset:

| Variable | If missing |
|---|---|
| `OPENCLAW_GATEWAY_TOKEN` | A random token is generated in the Secrets section. Put it in the env file to keep the same token every time you regenerate. |
| `ANTHROPIC_API_KEY`, `ANTIGRAVITY_API_KEY` | Left out of the YAML; log in with `claude` / `agy` later. |
| `OPENCLAW_GATEWAY_PASSWORD` | Only used with trusted-proxy auth (optional); a notice says when it is ignored. |

With trusted-proxy auth enabled, `OPENCLAW_GATEWAY_TOKEN` is not used (a notice says so) and is left out of the YAML: the gateway refuses to start with both.

**JSON keys.** The config can also be written by hand; the editor shows any problem when you open it.

| Key | Applies to | Default | Description |
|---|---|---|---|
| `image_source` | all | `inline` | `inline` (build on TrueNAS) or `prebuilt` (pull from a registry). |
| `image` | `prebuilt` | `ghcr.io/stefanomerinoderui/truenas-openclaw:latest` | Image reference. |
| `network_mode` | all | `proxy` | `proxy`, `bridge` or `host`. |
| `proxy_network` | `proxy`, `bridge` | `{"name": "ix-nginx-proxy-manager_default", "hostname": "openclaw"}` | Existing Docker network to join and the container's hostname/alias on it. Required in `proxy` mode (defaults apply); optional extra network in `bridge` mode; omit or `null` for none. |
| `public_urls` | `proxy` | **required** | URLs served by the proxy, e.g. `["https://openclaw.example.com"]`; become the allowed Control UI origins. |
| `port` | `bridge`, `host` | `18789` | Gateway port on TrueNAS. |
| `bind_addresses` | `bridge` | `["0.0.0.0"]` | IPs the port is published on (one mapping each). |
| `hosts` | `bridge`, `host` | **required** | TrueNAS hostnames/IPs used to open the Control UI. |
| `extra_origins` | `bridge`, `host` | `[]` | Additional allowed origin URLs. |
| `trusted_proxies` | all | `[]` | IPs or CIDRs of your reverse proxies (`gateway.trustedProxies`). Optional with token auth; required with `trusted_proxy`. Always written on deploy, so removing them here clears them in OpenClaw. |
| `trusted_proxy` | all | off | Trusted-proxy authentication, see [below](#trusted-proxy-authentication). Omit or `null` to keep gateway-token auth. |
| `storage` | all | **required** | `{"kind": "volumes"}` or `{"kind": "host", "base_path": "/mnt/<pool>/..."}` (`base_path` is required for `host`). |
| `timezone` | all | this machine's timezone | IANA name, e.g. `Europe/Madrid`. |
| `packages` | all | `["openclaw", "@anthropic-ai/claude-code"]` | Global pnpm packages to install on the tools volume. |
| `skip_update` | all | `false` | Don't update packages on start. |

A minimal config for the recommended setup:

```json
{
  "network_mode": "proxy",
  "public_urls": ["https://openclaw.example.com"],
  "storage": { "kind": "host", "base_path": "/mnt/tank/apps/openclaw" }
}
```

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

1. Open the Control UI and log in with the gateway token: the public URL served by your proxy in reverse proxy mode, otherwise `http://<truenas-host>:<port>/` (one of the origins you entered).
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

## Reverse proxy (Nginx Proxy Manager)

The recommended network mode puts the gateway behind a reverse proxy that runs as another TrueNAS app. The generated YAML declares the proxy's network as external and attaches only the gateway to it:

```yaml
networks:
  ix-nginx-proxy-manager_default:
    external: true
services:
  openclaw:
    hostname: openclaw
    networks:
      ix-nginx-proxy-manager_default:
        aliases: [openclaw]
```

`external: true` is only valid in the top-level `networks` block (Compose rejects it under a service). The gateway publishes no port on TrueNAS and is not on any other network, so it can only be reached by containers on the proxy's network; from outside, only the proxy's own ports (80/443) are exposed. The one-shot `update`/`doctor` stages keep the project's own network and don't join the proxy network.

- **Network name:** TrueNAS names app networks `ix-<app name>_default`; for an app called `nginx-proxy-manager` that is `ix-nginx-proxy-manager_default` (the default). List them with `sudo docker network ls`. The network must exist before you deploy, otherwise Compose stops with `network ... declared as external, but could not be found`: install and start the proxy app first.
- **Hostname:** set both as the container's hostname and as a network alias, because Docker's DNS resolves aliases (not hostnames) on user-defined networks. Use it as *Forward Hostname* in the proxy.
- In the proxy host, forward to `http://<hostname>:18789` (the port inside the container is always `18789`) and enable *Websockets Support*; use a certificate on the proxy host for HTTPS.
- The **public URL** you enter in the generator (e.g. `https://openclaw.example.com`) is added to the Control UI allowed origins; without it the UI rejects the origin.
- To change either value later, edit the app's YAML or regenerate it.

Reverse proxy mode isn't available together with host networking (a container on the host network can't join other networks). In bridge mode you can still join the proxy network in addition to the published port, e.g. to bind the published port to `127.0.0.1` only for local access.

## Trusted-proxy authentication

By default the Control UI is protected by the gateway token. Behind an identity-aware proxy (oauth2-proxy, Pomerium, Authelia, ...), you can instead let the proxy authenticate users and pass their identity in a header ([OpenClaw docs](https://docs.openclaw.ai/gateway/trusted-proxy-auth)). It is **opt-in**: a plain Nginx Proxy Manager proxy host doesn't authenticate anyone, so the token stays the default.

Fill in *Trusted proxies* and turn on *Delegate authentication to the reverse proxy* in the generator's Authentication section, or set `trusted_proxies` and a `trusted_proxy` object in the JSON config:

```json
{
  "network_mode": "proxy",
  "public_urls": ["https://openclaw.example.com"],
  "trusted_proxies": ["10.0.0.1"],
  "trusted_proxy": {
    "user_header": "x-auth-request-email",
    "required_headers": ["x-forwarded-proto", "x-forwarded-host"],
    "allow_users": ["me@example.com"]
  },
  "storage": { "kind": "volumes" }
}
```

| Key | Default | OpenClaw setting | Description |
|---|---|---|---|
| `trusted_proxies` (top level) | **required** here | `gateway.trustedProxies` | The IP(s) your proxy connects from. Requests from other sources are rejected. CIDRs are accepted, but every address in the range can then assert any user identity, so list only your proxy's own IP(s). `0.0.0.0/0` and `::/0` are refused. Configs from earlier versions with `trusted_proxy.proxies` still load; saving moves the list to `trusted_proxies`. |
| `user_header` | **required** | `gateway.auth.trustedProxy.userHeader` | Header carrying the authenticated identity, e.g. `x-auth-request-email` (oauth2-proxy) or `x-pomerium-claim-email` (Pomerium). Stored lowercase. The editor pre-fills `x-forwarded-user`. |
| `required_headers` | `[]` | `...requiredHeaders` | Headers that must be present and non-blank. The editor pre-fills `x-forwarded-proto, x-forwarded-host`, as in the OpenClaw example. |
| `allow_users` | `[]` | `...allowUsers` | Only these identities get in. **Empty allows every user the proxy authenticates**, so set it. |
| `allow_loopback` | `false` | `...allowLoopback` | Accept a same-host loopback proxy (bridge/host mode). Loopback must also be in `trusted_proxies`. |
| `device_auto_approve` | off | `...deviceAutoApprove` | `{"enabled": true, "scopes": ["operator.read"]}`: let authenticated proxy users enroll UI devices. Without `scopes`, OpenClaw's defaults apply (`operator.read`, `operator.write`, `operator.approvals`, `operator.questions`); `openclaw security audit` flags `operator.admin` as critical. |
| `identity_scopes` | `{}` | `gateway.auth.identityScopes` | Per-identity operator scopes, e.g. `{"admin@example.com": ["operator.admin"]}`. |

All of them can be set in the Authentication section; the advanced ones are below the allowed users.

**What changes in the YAML**
- `OPENCLAW_GATEWAY_TOKEN` is not set (the gateway fails to start when a shared token and trusted-proxy auth are both configured). `OPENCLAW_GATEWAY_PASSWORD` is passed through if provided.
- The settings are passed as `OPENCLAW_CONFIG_OVERRIDES` and applied by the `doctor` stage with `openclaw config set` on **every deploy**, so a regenerated YAML always wins. The auth mode (`trusted-proxy` or `token`) and `gateway.trustedProxies` (possibly empty) are always written, so switching back to token auth or removing proxies works without manual cleanup. If applying them fails, the stack stops instead of starting the gateway with the wrong authentication.
- This relies on the updated `docker-entrypoint.sh`: with a prebuilt image, rebuild and push it first (the GitHub Actions workflow does this on push to `main`).

**Setting it up**
1. Find your proxy's IP and use it in *Trusted proxies* (`trusted_proxies`, `gateway.trustedProxies`):
   - Proxy container on the shared network (reverse proxy mode):

     ```sh
     sudo docker ps --format '{{.Names}}'   # find the proxy container
     sudo docker inspect -f '{{(index .NetworkSettings.Networks "ix-nginx-proxy-manager_default").IPAddress}}' <proxy container>
     ```

   - Proxy on the TrueNAS host itself, reaching a published port (bridge mode): it usually connects from the Docker network's gateway IP, e.g. `172.17.0.1`.

   A container's IP can change when it is recreated, and the gateway then rejects the proxy. Pin a static IP for the proxy container, or regenerate and redeploy after a change. For a quick fix inside the running app, the same setting takes a JSON list: `openclaw config set gateway.trustedProxies '["10.0.0.5"]'`. The next deploy overwrites it with the value from the YAML, so update the generator config too. OpenClaw rejects sources that match the gateway host's own addresses.
2. The proxy must send the identity header and **overwrite** `X-Forwarded-For` and the other `x-forwarded-*` headers rather than append to them. A request from a trusted proxy without forwarded client headers is answered with `403 proxy_attribution_required`.
3. Never expose the gateway port directly: the default proxy mode publishes none. In bridge/host mode, firewall the port so the proxy is the only path to it.
4. `openclaw security audit` reports trusted-proxy auth as a critical finding by design.

The gateway itself decides on the identity over its WebSocket protocol, so a plain `curl` can't show a clear accept/reject; test by opening the Control UI through the proxy with an allowed and a disallowed user.

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
| Gateway exits right after switching to trusted-proxy auth | Check `OPENCLAW_GATEWAY_TOKEN` isn't set in the app's environment (regenerate the YAML; the generator omits it). The gateway refuses to start with a token and trusted-proxy auth together. |
| `403 proxy_attribution_required` | The request comes from a trusted proxy but without forwarded client headers. Make the proxy send (and overwrite) `X-Forwarded-For`. |
| Users are rejected although the proxy authenticates them | Compare the proxy's identity header with `user_header` and the value with `allow_users`; check *Trusted proxies* holds the proxy's current IP (it may have changed if the proxy container was recreated) and `required_headers` are all sent. |
| OpenClaw: `CLI backend claude-cli executable cannot be bound to one durable absolute owner` | OpenClaw only runs a `claude` it can fingerprint (the native binary), not pnpm's `#!/bin/sh` shim. The `update` stage links the real binary into `~/.local/native-bin`, which is first in `PATH`. Images and YAMLs from before this fix don't do that: regenerate the YAML (build mode) or pull the rebuilt image (prebuilt mode), then redeploy. Check with `docker exec <container> sh -c 'readlink -f "$(command -v claude)"'`, which should end in `claude.exe`. |
| Port conflict on deploy | Choose another gateway port in the generator, or check that *Bind addresses* doesn't list the same address as another app. |
| `network ... declared as external, but could not be found` | The proxy network doesn't exist yet (or has another name). Start the proxy app first and check `sudo docker network ls`. |
| `Ignored build scripts` / `ERROR: logging before google.Init` in `update` logs | Harmless; see the main [README](README.md#troubleshooting). |

## Development

The generator lives in `truenas/`: `compose_builder.py` builds the Compose document (pure logic), `settings.py` holds the non-secret settings, their JSON form and validation, `draft.py` converts between the editor's form values and the config and maps problems to fields, `tui.py` is the Textual app, `envfile.py` loads the secrets, `validators.py` has the input checks, `clipboard.py` detects the clipboard backend and copies, and `generate_yaml.py` is the command-line entry point. Tests and lint:

```sh
cd truenas
uv run --no-project --with pytest --with pyyaml --with textual --with python-dotenv pytest
ruff check . && ruff format --check .
```
