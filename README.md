# Useful commands
To locally run an MCP server (stdio transport): `uv run --env-file .env my_server.py`

To locally run an MCP server over HTTP transport: `uv run --env-file .env fastmcp run my_server.py --transport http --port 8000`
(the server is then reachable at `http://localhost:8000/mcp` — use this URL when adding a Custom Connector in Claude Desktop)

To locally test an MCP client connecting to a local MCP server: `uv run my_client.py`

To test FastMCP "apps": `uv run fastmcp dev apps my_server.py`

# Developer notes
## NOT NEEDED UNLESS USING STDIO TRANSPORT
For testing in the Claude Desktop app, add the following to the `claude_desktop_config.json` file (typically found inside of `AppData / Roaming` somewhere).

```json
"mcpServers": {
    "seth-super-cool-mcp": {
      "command": "uv",
      "args": [
        "run",
        "--project",
        "C:\\seth-data\\fastmcp-playground",
        "--env-file",
        "C:\\seth-data\\fastmcp-playground\\.env",
        "fastmcp",
        "run",
        "C:\\seth-data\\fastmcp-playground\\my_server.py"
      ]
    }
  }
```