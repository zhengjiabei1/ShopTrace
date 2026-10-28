# Deployment

The server runs Python and Chromium. Users access an authenticated workbench, and task records are saved to a persistent volume. The repository includes a Dockerfile, Compose configuration, an HTTPS reverse-proxy example, and a single-user authentication entry point.

## Processes and access

- `python -m webagent serve` is the local entry point and listens on loopback.
- `python -m webagent.hosted` enables single-user hosting. It requires a public HTTPS origin, a username, and a SHA-256 hash of a separate login password. Missing settings stop startup; there is no default password.
- The public listener defaults to `0.0.0.0:8080`. Workbench pages, APIs, exports, and screenshots require HTTP Basic authentication. Terminate TLS at a controlled reverse proxy; do not expose the HTTP port directly. Unauthenticated `/healthz` returns only `{"ready":true}`.
- Chromium uses an internal listener on `127.0.0.1:8777`, allowing only GET/HEAD under `/sandbox/`. Docker does not publish this port. Internal access rejects APIs and writes.
- Public writes require an `Origin` matching `WEBAGENT_PUBLIC_ORIGIN`. The gateway checks Host, Origin, and login before rewriting the internal address. Client IP and `X-Forwarded-*` do not bypass authentication, and CORS has no wildcard origin.
- Optional `SHOPTRACE_REPORT_DIR` mounts generated reports read-only at `/reports/`, behind the same login. The Docker image includes the build-time `portal/dist` snapshot; rebuild it after updating reports.
- Login credentials are not forwarded to the internal app or saved in model trajectories. Use a password of at least 16 characters, different from `LLM_API_KEY`. Clearing cached Basic credentials may require closing the browser session.
- This is a single-user workbench. Logged-in requests share all history and API usage; do not share its password with untrusted users.

## Docker and an HTTPS reverse proxy

Work from the repository root on the target host. `deploy/Dockerfile.dockerignore` includes dependencies, application source, and generated report snapshots, excluding Git history, work directories, and credential files.

```bash
cp deploy/runtime.env.example deploy/runtime.env
chmod 600 deploy/runtime.env
```

Edit `deploy/runtime.env` in a controlled editor or platform Secret settings:

| Setting | Purpose |
|---|---|
| `WEBAGENT_PUBLIC_ORIGIN` | Public origin, such as `https://shoptrace.example.com`, without a path or trailing slash |
| `WEBAGENT_AUTH_USER` | Login username, without a colon |
| `WEBAGENT_AUTH_PASSWORD_SHA256` | Lowercase hexadecimal SHA-256 hash of a separate password of at least 16 characters |
| `LLM_BASE_URL` / `LLM_MODEL` | Model API endpoint and exact model ID |
| `LLM_API_KEY` | Model credential injected into the backend |

Keep credentials out of `.example` files and command arguments. Git ignores `deploy/runtime.env`.

```bash
docker compose -f deploy/compose.yaml build
docker compose -f deploy/compose.yaml up -d
docker compose -f deploy/compose.yaml logs --tail=50
```

Compose binds the public HTTP entry point to host `127.0.0.1:8080` and sets a non-root user, CPU and memory limits, and log rotation. Set the domain in `deploy/Caddyfile.example`, point DNS at the host, and run Caddy or another TLS reverse proxy. Expose only the HTTPS proxy; port `8777` needs no mapping.

Records are stored in the `shoptrace-data` volume; back it up before upgrades. Use one app process per data directory, rather than multiple workers or replicas writing to the same SQLite database.

## Deployment checks

1. Without login, confirm that workbench pages, APIs, screenshots, and exports return 401. `/healthz` must not disclose configuration.
2. After login, run `browser_smoke` and check screenshots, export, cancellation, and history after refresh. It makes no model calls.
3. In the container, run `python -m webagent doctor` and a model preflight with the service's configuration.
4. After preflight passes, run a small batch with `api`, inspect its results, and save screenshots and trajectories.
5. Restart the container and check that history persists. Confirm that SIGTERM stops both listeners and cleans up in-flight browsers.
