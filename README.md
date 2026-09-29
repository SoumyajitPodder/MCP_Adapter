# MCP_Adapter

An MCP adapter that puts stable, versioned tools in front of legacy telecom backends.

| Folder | What it is |
| --- | --- |
| [`contract_testing/`](contract_testing/README.md) | Execution and verification layer (LLD §5–9): contract CI, golden-task regression, duplicate prevention, correlation-ID logging, access control |
| [`kernel/`](kernel/) | Shared kernel draft: error codes, call context, response `_meta`, pipeline stage interface, shape model. Used by every component; changes need both owners |

Python components share one uv workspace (`pyproject.toml`, `uv.lock`). Secrets go in a git-ignored `.env` here; copy [`.env.example`](.env.example).

```bash
cd contract_testing
uv sync
uv run pytest
```
