# mcp-servers

First-party MCP servers that ship inside the goose image.

Anything with a `pyproject.toml` in a direct subdirectory here is installed with
`uv tool install` at image build time and lands on `PATH`. A server placed here
needs no `--from` path and no runtime dependency resolution — which matters,
because `uvx --with <dep>` reaches PyPI on every extension start, and a cluster
that blocks egress fails there rather than at deploy.

```
mcp-servers/
  veza-query-mcp/
    pyproject.toml
    uv.lock
    src/veza_query_mcp/
```

## Adding one

Copy the project in, source only:

```bash
rsync -a --exclude .venv --exclude dist --exclude .pytest_cache \
      --exclude '__pycache__' --exclude .git \
      /path/to/veza-query-mcp/ mcp-servers/veza-query-mcp/
```

`.venv/`, `dist/` and `__pycache__/` are excluded by `.dockerignore` as well, so
a stray local virtualenv cannot reach the build context. Commit `uv.lock` for reproducible local
development. Note that `uv tool install` does **not** consume it — the image
build resolves dependencies fresh — so a pinned image needs pinned constraints
in `pyproject.toml`, not just a lockfile.

## Using one

Reference it by its console script name, not by path:

```yaml
extensions:
  veza:
    type: stdio
    name: veza
    cmd: veza-query-mcp
    args: []
    timeout: 300
```

The script name comes from `[project.scripts]` in the server's `pyproject.toml`.

## Notes

- `requires-python` must be satisfied by the interpreter baked into the image.
  That is `PYTHON_VERSION` in `Dockerfile.server`, currently 3.12.
- Secrets belong in the environment, never here. These directories are baked
  into the image and readable by anyone who can pull it.
