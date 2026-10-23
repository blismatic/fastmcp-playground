# Setup
## Claude Desktop
### Local STDIO with developer bypass
In your `claude_desktop_config.json` file (which can be found by opening Claude Desktop -> settings -> This computer -> Developer -> Edit config), add the following:
```json
{
    "mcpServers": {
        "lightning-docs-mcp": {
            "command": "uv",
            "args": [
                "run",
                "--directory", "C:\\<path>\\<to>\\<repository>",
                "--env-file", ".env.dev",
                "fastmcp", "run", "my_server.py",
                "--transport", "stdio"
            ]
        }
    }
}
```

Make sure that in the `.env.dev` file, `MCP_AUTH_ENABLED` is set to `false`, and `MCP_DEV_SUB` has an Auth0 sub that you want to use.

### HTTP with actual Auth0 authentication
Start the server manually with the following command: `uv run --env-file .env fastmcp run my_server.py --transport http --port 8000` (the server is then reachable at `http://127.0.0.1:8000/mcp`) (note: `127.0.0.1:8000` is the same as `localhost:8000`)

In your `claude_desktop_config.json` file (which can be found by opening Claude Desktop -> settings -> This computer -> Developer -> Edit config), add the following:
```json
{
    "mcpServers": {
        "lightning-docs-mcp": {
            "command": "uvx",
            "args": [
                "fastmcp-remote",
                "http://127.0.0.1:8000/mcp"
            ]
        }
    }
}
```

# Useful commands
Run unit tests: `uv run pytest` (no MongoDB or AWS credentials are needed)

Check a user's permissions straight from DynamoDB: `uv run --env-file .env python -c "from identity import get_user_permissions; print(get_user_permissions('auth0|abc123...'))"`

Run the my_client.py script against a locally running HTTP transport server to test that Auth0 login actually works: `uv run --env-file .env my_client.py`

To test FastMCP "apps": `uv run fastmcp dev apps my_server.py`

# Developer notes
## Logging
All logs go to **stderr** (stdout carries the MCP protocol on stdio).

- **Audit lines** (`audit_logging.py`): one pure-JSON line per event, for every `tools/call` and `resources/read`:
  - `request_start`: `time` (UTC), `sub`, `method`, and `payload` (the tool name and arguments, including the pipeline), cut off at 4 KB;
  - `report_result` (from `run_report`): `returned_count` and `truncated`. Never the report data;
  - `request_success` with `duration_ms`, or `request_error` with `duration_ms` and `error` (the message the model saw, e.g. a guard or rate-limit rejection).
- **Our own modules' logs**: `logger = logging.getLogger(__name__)` then `logger.debug(...)` / `logger.info(...)`. `LOG_LEVEL` (default `INFO`) applies to our modules only; set `LOG_LEVEL=DEBUG` in `.env.dev` to see debug lines. Third-party libraries stay at WARNING.
- **FastMCP's own messages** (e.g. "Starting MCP server") keep FastMCP's formatting, with their level controlled by `FASTMCP_LOG_LEVEL`.
- **Log files (optional, local dev):** set `LOG_DIR=logs` (e.g. in `.env.dev`) to also write `logs/audit.jsonl` (the audit lines, pure JSON) and `logs/server.log` (everything else, including FastMCP's messages). A relative `LOG_DIR` is relative to the project folder. The files aren't rotated, so delete them whenever you like; `logs/` is gitignored.

Audit logs contain Auth0 `sub`s and the values users searched for in pipelines. Decide retention and access when choosing where deployed logs are stored.

## AWS credentials
The server reads DynamoDB with whatever credentials boto3 finds. Locally that's the profile named by `AWS_PROFILE` in `.env` (the region also comes from the profile).

### One-time setup: the dev role
1. IAM > Policies > Create policy > JSON (use your real `DYNAMO_*` table names, account ID, and region):
   ```json
   {
     "Version": "2012-10-17",
     "Statement": [{
       "Effect": "Allow",
       "Action": ["dynamodb:BatchGetItem", "dynamodb:Query", "dynamodb:DescribeTable"],
       "Resource": [
         "arn:aws:dynamodb:us-east-1:<account-id>:table/<UsersTable>",
         "arn:aws:dynamodb:us-east-1:<account-id>:table/<UsersTable>/index/<Auth0UserIdIndex>",
         "arn:aws:dynamodb:us-east-1:<account-id>:table/<UserOrganizationsTable>",
         "arn:aws:dynamodb:us-east-1:<account-id>:table/<UserOrganizationsTable>/index/*",
         "arn:aws:dynamodb:us-east-1:<account-id>:table/<OrganizationsTable>"
       ]
     }]
   }
   ```
   Name it `<some-policy-name-here>`. The same policy goes on the production task role later.
2. IAM > Roles > Create role > Trusted entity: **AWS account** > **This account** > attach the policy above > name it `<some-role-name-here>`.
3. Add a profile to `~/.aws/config` that assumes the role using your own credentials (no new keys to manage):
   ```ini
   [profile <profile-name-here>]
   role_arn = arn:aws:iam::<account-id>:role/some-role-name-here
   source_profile = <your personal profile>
   region = us-east-1
   ```
4. Set `AWS_PROFILE=<profile-name-here>` in `.env`.

### AWS CLI cheat sheet
Commands are bash syntax (Git Bash). In Windows PowerShell 5.1, JSON arguments need their inner quotes escaped (`'{\"id\":{\"S\":\"auth0|abc\"}}'`), or use Git Bash instead.

| Task | Command |
|---|---|
| CLI installed? | `aws --version` |
| List configured profiles | `aws configure list-profiles` |
| What credentials/region a profile resolves to (keys masked) | `aws configure list --profile <name>` |
| Who am I? (works with any valid credentials) | `aws sts get-caller-identity --profile <name>` |
| Set up a profile with access keys | `aws configure --profile <name>` |
| Set up / refresh an SSO profile | `aws configure sso` / `aws sso login --profile <name>` |

## Deployment / Update Flow
1. Make changes on local copy of repo
2. Run unit tests locally: `uv run pytest`
3. Commit and push changes
4. Remotely connect into the hosting server
5. Login as the `mcp` user: `sudo -iu mcp`
6. Navigate to the directory that the server code is stored in: `cd ~/app`
7. Pull the changes from git: `git pull`
8. Install any new or updated packages: `uv sync --frozen --no-dev`
9. Logout as the `mcp` user: `exit`
10. Restart the mcp server: `sudo systemctl restart lightning-docs-mcp`
11. Check that the mcp server is running:
    1.  Look for a green "active (running)" text: `sudo systemctl status lightning-docs-mcp`
    2.  Look for no errors in the immediate server terminal: `sudo journalctl -u lightning-docs-mcp -n 30 --no-pager`
    3.  Look for valid JSON returned from: `curl -s http://127.0.0.1:8000/.well-known/oauth-protected-resource/mcp`

## How to rollback the server to a previous version
1. `sudo -iu mcp`
2. `cd ~/app`
3. `git checkout <good-commit-id>`
4. `uv sync --frozen --no-dev`
5. `exit`
6. `sudo systemctl restart lightning-docs-mcp`

## How to watch the live server logs
1. For just the new lines from the terminal: `sudo journalctl -u lightning-docs-mcp -f`
2. With some recent history before following: `sudo journalctl -u lightning-docs-mcp -n 50 -f`
3. To safely stop watching: `CTRL + C`

Once whatever problem occurred is fixed, just `git checkout main` and then go back to the `git pull` step up above.

# TODO in order
- [x] Auth using Auth0 (verified end to end over HTTP with a staff Microsoft login, and over STDIO with a non-staff dev sub)
  - [x] Get staff status / list of catalogs via Auth0 "sub" claim and Dynamo tables (`identity.py`)
    - [x] Essentially want a function that takes in an Auth0 "sub", and outputs an object with the format: `{ "isStaff": false, "catalogs": ["catalogA", "catalogB", "catalogC"] }` (`get_user_permissions`)
  - [x] Add a hardcoded $match stage to the report generation to allow safe data retrieval (`pipeline_guard.py`)
  - [x] Look into how to set / determine the expiration date from an Auth0 token (`whoami` tool)
  - [x] Create a dedicated MongoDB user with the `read` role scoped to the one collection
- [ ] Rate limiting
  - [ ] Need IP-based limits on the public /register endpoint, configured on the deployment server (AWS, CloudFlare, Prefect Horizon, etc...). Also worth covering the other public OAuth routes: /authorize, /consent, /token, /auth/callback
  - [x] Have "sub"-based limits on the /run_report endpoint and a shared concurrent limit that is under the 40 threads limit (`report_limits.py`: per user 2 at once and 30 per minute; 30 at once server-wide)
- [x] Logging (`audit_logging.py`: one JSON audit line per tool call / resource read, plus `LOG_LEVEL` for our own `logger.debug` / `logger.info`; see Developer notes)
- [x] Optimize the time it takes to build reports
  - [x] Potentially implement Progress Reporting so that users can see their reports being built (https://gofastmcp.com/servers/progress)
- [ ] MongoDB stuff
  - [ ] Bulk populate the entire schemaJsonRecords collection
  - [x] Build the tool to automatically keep the schemaJsonRecords collection updated
  - [x] Update the daily double checker tool to keep the schemaJsonRecords collection updated
- [ ] Deployment
  - [x] Getting the server hosted on an actual HTTPS host somewhere
  - [ ] Look into how to get the MCP server added / packaged for things like Claude, Gemini, ChatGPT, etc...
  - [ ] Possible Claude plugin so people can run slash commands for it?