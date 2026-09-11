# Useful commands
To locally run an MCP server: `uv run my_server.py`

To locally test an MCP client connecting to a local MCP server: `uv run my_client.py`

To test FastMCP "apps": `uv run fastmcp dev apps my_server.py`

# Developer notes
For testing in the Claude Desktop app, add the following to the `claude_desktop_config.json` file (typically found inside of `AppData / Roaming` somewhere)
```json
"mcpServers": {
    "seth-super-cool-mcp": {
      "command": "uv",
      "args": [
        "run",
        "--with",
        "fastmcp[apps]",
        "fastmcp",
        "run",
        "C:\\seth-data\\fastmcp-playground\\my_server.py"
      ]
    }
  }
```