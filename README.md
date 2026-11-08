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

## Getting the memoryweb binary

This package wraps an existing `memoryweb` MCP server; it ships no server code.
Build or download one from the upstream project:

```bash
git clone https://github.com/corbym/memoryweb.git
cd memoryweb && go build -o memoryweb .
install -Dm755 memoryweb ~/.local/bin/memoryweb   # any dir on PATH works
```

Prebuilt binaries may also be attached to upstream releases. Verify with
`memoryweb --version` before enabling the plugin.

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

### Embedding model

`bge-m3-8k` is a local alias for the public [bge-m3](https://ollama.com/library/bge-m3)
embedding model with its 8k context window baked in, so embedding calls need no runtime
options. Build it from the public model:

```bash
ollama pull bge-m3
printf 'FROM bge-m3\nPARAMETER num_ctx 8192\n' > Modelfile
ollama create bge-m3-8k -f Modelfile
```

Any other Ollama embedding model works too — point `MEMORYWEB_EMBED_MODEL` at it; a
database must keep using the model it was built with (dimensions must match).

## Memory provider module (optional)

Besides the portable MCP package, this repository ships a native
**MemoryProvider** implementation in [`provider/`](provider/README.md): activate
it with `memory.provider: memoryweb` to make the graph the agent's memory
backend — live `memoryweb_*` tools through the provider tool surface, a domain
snapshot in the system prompt, background semantic recall between turns,
transient session traces via `sync_turn`, and fail-closed pre-compression
checkpoints (`compression.checkpoint_required: true` supported). The two modules
are independent; either or both can be installed.

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
