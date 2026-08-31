Put `config.yaml` here. It is mounted read-only at
`/home/goose/.config/goose/` inside the container.

The one thing that is easy to get wrong: the `compliancecow` extension must
list the headers goose is allowed to forward.

    allowed_headers:
      - Authorization
      - X-Cow-Security-Context

Without it, §4 header forwarding is off. Nothing fails loudly — goose makes the
MCP call, cow-mcp receives no tenant context, and returns 401. The symptom shows
up as empty tool results, not as a goose error.
