# memoryweb memory provider

Native Hermes `MemoryProvider` implementation that turns a local memoryweb
graph server into the agent's memory backend.

## Install

The portable MCP package in this repository's root and this provider are two
independent modules — you can run either or both. To use the provider:

```bash
# 1) the memoryweb binary on PATH (same requirement as the portable package)
ln -s /opt/mcp/memoryweb/memoryweb /usr/local/bin/memoryweb   # example

# 2) copy this directory in as a user plugin (name must be `memoryweb`)
cp -r provider/ ~/.hermes/plugins/memoryweb/

# 3) activate it
hermes config set memory.provider memoryweb
```

Restart Hermes. The plugin loader discovers `$HERMES_HOME/plugins/memoryweb/`
(bundled `plugins/memory/` wins on a name clash by design).

## Environment

Same three variables as the portable package, all optional:

| Variable | Provider default when unset |
|---|---|
| `MEMORYWEB_DB` | `<hermes_home>/.memoryweb.db` |
| `OLLAMA_HOST` | server default (`http://localhost:11434`) |
| `MEMORYWEB_EMBED_MODEL` | server default model |

The database path is bound per profile from `initialize()`'s `hermes_home`, so
a multiplexed gateway keeps one graph per profile.

## What each hook does

| ABC surface | Implementation |
|---|---|
| `get_tool_schemas` / `handle_tool_call` | the server's live tool set, fetched at startup via MCP `tools/list`, exposed as `memoryweb_<tool>` and dispatched verbatim over one long-lived stdio connection |
| `system_prompt_block` | static pointer block + the domain snapshot captured once at initialize (durable recall rides `prefetch`, per the ABC contract) |
| `queue_prefetch` / `prefetch` | background semantic search after each turn, returned as recall context for the next turn |
| `sync_turn` | one transient `hermes/sessions` session-trace node per session (upsert; subagent/cron/flush contexts skip writes) |
| `on_pre_compress` | checkpoint API v2 (fail-closed): a durable single-file `memoryweb backup` snapshot is committed under `memoryweb-checkpoints/` next to the database before returning; failures raise, so `compression.checkpoint_required: true` blocks the lossy rewrite instead of losing the transcript |
| `shutdown` | closes the stdio server cleanly |

Non-primary agent contexts (`subagent`, `cron`, `flush`) never write.

## License

MIT — see the repository [LICENSE](../LICENSE).
