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
Start the server manually with the following command: `uv run --env-file .env fastmcp run my_server.py --transport http --port 8000` (the server is then reachable at `http://localhost:8000/mcp`)

In your `claude_desktop_config.json` file (which can be found by opening Claude Desktop -> settings -> This computer -> Developer -> Edit config), add the following:
```json
{
    "mcpServers": {
        "lightning-docs-mcp": {
            "command": "uvx",
            "args": [
                "fastmcp-remote",
                "http://localhost:8000/mcp",
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
## AWS credentials
The server reads DynamoDB with whatever credentials boto3 finds. Locally that's the profile named by `AWS_PROFILE` in `.env` (the region also comes from the profile).

### One-time setup: the dev role
1. IAM → Policies → Create policy → JSON (use your real `DYNAMO_*` table names, account ID, and region):
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
2. IAM → Roles → Create role → Trusted entity: **AWS account** → **This account** → attach the policy above → name it `<some-role-name-here>`.
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

# TODO in order
- [x] Auth using Auth0 (verified end to end over HTTP with a staff Microsoft login, and over STDIO with a non-staff dev sub)
  - [x] Get staff status / list of catalogs via Auth0 "sub" claim and Dynamo tables (`identity.py`)
    - [x] Essentially want a function that takes in an Auth0 "sub", and outputs an object with the format: `{ "isStaff": false, "catalogs": ["catalogA", "catalogB", "catalogC"] }` (`get_user_permissions`)
  - [x] Add a hardcoded $match stage to the report generation to allow safe data retrieval (`pipeline_guard.py`)
  - [x] Look into how to set / determine the expiration date from an Auth0 token (`whoami` tool)
  - [x] Create a dedicated MongoDB user with the `read` role scoped to the one collection
- [ ] Rate limiting
- [ ] Logging
- [x] Optimize the time it takes to build reports
  - [x] Potentially implement Progress Reporting so that users can see their reports being built (https://gofastmcp.com/servers/progress)
- [ ] MongoDB stuff
  - [ ] Bulk populate the entire schemaJsonRecords collection
  - [ ] Build the tool to automatically keep the schemaJsonRecords collection updated
  - [ ] Update the daily double checker tool to keep the schemaJsonRecords collection updated
- [ ] Deployment
  - [ ] Getting the server hosted on an actual HTTPS host somewhere
  - [ ] Look into how to get the MCP server added / packaged for things like Claude, Gemini, ChatGPT, etc...
  - [ ] Possible Claude plugin so people can run slash commands for it?