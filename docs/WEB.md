# Caliper web app

A ChatGPT-style chat UI over the Caliper agent: streamed process (plan → tool runs →
trust verdict), results rendered inline, chat-history sidebar, and a read-only data
browser/search. **Config-driven** — the same code runs locally for testing and on a lab
server for deployment; only environment variables change. No site specifics or secrets
live in the repo.

## Configuration (all via environment)

| Var | Meaning |
|-----|---------|
| `CALIPER_WORKSPACE` | confined write directory — all outputs/temp live here |
| `CALIPER_DATA_ROOT` | read-only root the browser/search is limited to |
| `CALIPER_PACK` | domain pack (default `bio`) |
| `CALIPER_PROVIDER` | `anthropic` \| `openai` \| `mock` |
| `ANTHROPIC_API_KEY` | server-side only; never sent to the browser |
| `CALIPER_WEB_PASSWORD` | if set, the UI requires this password (else dev-open) |

## Run locally (offline, no key)

```bash
pip install -e ".[web]"
CALIPER_PROVIDER=mock CALIPER_DATA_ROOT=examples CALIPER_WORKSPACE=/tmp/cw caliper-web
# open http://127.0.0.1:8000
```

## Deploy to a lab server (Option B — runs on their machine)

Everything lives under the user's own directory; their data is read-only; nothing of
theirs leaves the machine. Public access is via a Cloudflare Tunnel — **no inbound port
is opened**.

```bash
# on the lab server, inside the workspace (e.g. /home/HDD/HDD7/jie_caliper)
#   .env (chmod 600, NOT in git):
#     ANTHROPIC_API_KEY=...            # the dedicated key
#     CALIPER_WORKSPACE=/home/HDD/HDD7/jie_caliper
#     CALIPER_DATA_ROOT=/home/HDD/HDD7
#     CALIPER_PACK=bio
#     CALIPER_PROVIDER=anthropic
#     CALIPER_WEB_PASSWORD=...         # or rely on Cloudflare Access
set -a; source .env; set +a
caliper-web    # serves on 127.0.0.1:8000

# expose publicly with no open inbound port + TLS + login:
cloudflared tunnel --url http://127.0.0.1:8000
#   (or a named tunnel + Cloudflare Access for managed logins)
```

## Security model

- **Read anywhere, write only in the workspace** — enforced by the executor (guard +
  optional bubblewrap). The agent can browse/search `CALIPER_DATA_ROOT` read-only but
  cannot modify or delete the lab's data.
- **The API key is server-side only** — the browser never receives it; it is not in the
  repo, commits, or the frontend bundle.
- **Auth:** set `CALIPER_WEB_PASSWORD` and/or front it with Cloudflare Access. Do not
  reuse the lab's SSH password as the web gate.
- The data browser/search is confined to `CALIPER_DATA_ROOT`; path traversal is rejected.

Get PI sign-off (and an IT heads-up for any public exposure) before sharing the URL.
