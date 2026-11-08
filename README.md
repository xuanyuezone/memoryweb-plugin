# memoryweb-plugin

A [Hermes Agent](https://github.com/NousResearch/hermes-agent) plugin that connects the
agent to a local memoryweb memory-graph server over the
Model Context Protocol (stdio).

The package is a **portable Agent Plugins v1** integration: `plugin.json` + `mcp.json`,
pure configuration, no code. Once installed and enabled, every tool the memoryweb server
exposes (domains, orient, search, recall, remember, connect, audit, … — 18 tools on
memoryweb v1.65.0) is available in the session under the same `mcp__memoryweb__<tool>`
names a hand-written `mcp_servers.memoryweb` config block produces.

## Requirements

- Hermes Agent **≥ 0.21.6** (first release line supporting portable Agent Plugins packages).
- The `memoryweb` binary on `PATH` for the user Hermes runs as (stdio server commands must
  be a bare executable token, so an absolute path cannot be declared in `mcp.json`).

## Install

```bash
hermes plugins install https://github.com/xuanyuezone/memoryweb-plugin.git --no-enable
hermes plugins list          # shows memoryweb, disabled
hermes plugins enable memoryweb
```

Portable packages stay **disabled after install** until explicitly enabled — installing
alone never changes session behavior. Tools appear in the next session (or after
`/reload-plugins` where available).

## Environment

The server environment is pure passthrough; the package hard-codes no paths:

| Variable | Unset fallback (memoryweb default) |
|---|---|
| `MEMORYWEB_DB` | `~/.memoryweb.db` |
| `OLLAMA_HOST` | `http://localhost:11434` (semantic search degrades to lexical when unreachable) |
| `MEMORYWEB_EMBED_MODEL` | server default model |

Each entry is declared as `"${VAR}"` in `mcp.json`: the value is interpolated from the
user's shell environment or the profile `.env`, and an unset variable is dropped entirely
(the server never sees a literal `${VAR}`). To pin values for a Hermes home, put them in
`~/.hermes/.env`:

```
MEMORYWEB_DB=/home/you/.hermes/memoryweb.db
OLLAMA_HOST=http://localhost:11434
MEMORYWEB_EMBED_MODEL=bge-m3-8k
```

## Migrating from a native config block

On a host that already runs memoryweb via `mcp_servers.memoryweb` in `config.yaml`, native
config wins over a portable package with the same server name, so enabling this plugin
changes nothing until the native block is removed:

1. Install and enable the plugin (still shadowed by the native entry).
2. Move the three environment values from the native block into `.env` as above.
3. Delete the `mcp_servers.memoryweb` block and restart the gateway.

## Testing / acceptance reproduction

The acceptance checks for this package run against a self-made demo database — no
production data — and are reproducible with any CPython 3.9+ and the memoryweb binary:

```bash
python3 tests/make_demo_db.py --db /tmp/demo.db      # build the fixture
python3 tests/mcp_min_client.py memoryweb domains '{"action":"list"}'
```

Inside a stock `nousresearch/hermes-agent` container with `memoryweb` on `PATH`:

1. **Config UI presence** — after `hermes plugins install <this-repo> --no-enable`,
   `hermes plugins list` (the payload the desktop/TUI plugins hub renders) shows the
   `memoryweb` entry.
2. **Tool parity with a direct connection** — run once with a native
   `mcp_servers.memoryweb` block and once with the plugin enabled, against the same demo
   database; the session tool list and read-tool outputs (domains/search/recall) match.
3. **No disturbance while disabled** — a home with the plugin installed but not enabled
   exposes no `mcp__memoryweb__*` tools and reports no load errors, identical to a home
   without the plugin.

## License

MIT — see [LICENSE](LICENSE).
