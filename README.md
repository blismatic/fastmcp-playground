# Useful commands
To locally run an MCP server (stdio transport): `uv run --env-file .env my_server.py`

To locally run an MCP server over HTTP transport: `uv run --env-file .env fastmcp run my_server.py --transport http --port 8000`
(the server is then reachable at `http://localhost:8000/mcp` — use this URL when adding a Custom Connector in Claude Desktop)

To locally test an MCP client connecting to a local MCP server: `uv run my_client.py`

To test FastMCP "apps": `uv run fastmcp dev apps my_server.py`

# Developer notes
## NOT NEEDED UNLESS USING STDIO TRANSPORT
For testing in the Claude Desktop app (with HTTP transport), add the following to the `claude_desktop_config.json` file (typically found inside of `AppData / Roaming` somewhere).

```json
"mcpServers": {
    "seth-super-cool-mcp": {
      "command": "uvx",
      "args": [
        "fastmcp-remote", "http://127.0.0.1:8000/mcp",
        "--auth", "none"
      ]
    }
  }
```

# TODO in order
- [ ] Auth using Auth0
  - [ ] Get staff status / list of catalogs via Auth0 "sub" claim and Dynamo tables (look into Boto3 library?) (https://gofastmcp.com/servers/dependency-injection#access-token)
    - [ ] Essentially want a function that takes in an Auth0 "sub", and outputs an object with the format: `{ "isStaff": false, "catalogs": ["catalogA", "catalogB", "catalogC"] }`
  - [ ] Add a hardcoded $match stage to the report generation to allow safe data retrieval
  - [ ] Look into how to set / determine the expiration date from an Auth0 token
- [ ] Rate limiting
- [ ] Logging
- [ ] Optimize the time it takes to build reports
  - [ ] Potentially implement Progress Reporting so that users can see their reports being built (https://gofastmcp.com/servers/progress)
- [ ] MongoDB stuff
  - [ ] Bulk populate the entire schemaJsonRecords collection
  - [ ] Build the tool to automatically keep the schemaJsonRecords collection updated
  - [ ] Update the daily double checker tool to keep the schemaJsonRecords collection updated
- [ ] Deployment
  - [ ] Getting the server hosted on an actual HTTPS host somewhere
  - [ ] Look into how to get the MCP server added / packaged for things like Claude, Gemini, ChatGPT, etc...
  - [ ] Possible Claude plugin so people can run slash commands for it?