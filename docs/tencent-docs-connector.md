# Tencent Docs MCP connector

Suyo v1.0 includes a built-in Tencent Docs connector for the official remote
MCP endpoint:

- Endpoint: `https://docs.qq.com/openapi/mcp`
- Personal-token page: `https://docs.qq.com/open/auth/mcp.html`
- Connector id: `tencent-docs`

## Setup

1. Open **Settings > Plugins & Skills > Connectors**.
2. Find **Tencent Docs** and open the personal-token link.
3. Paste the personal token into Suyo and confirm.

The token is persisted by the existing `McpTokenStore` / `CredentialStore`
chain. It is not stored in workspace connector JSON and is never returned by
the connectors API. Tencent's contract requires the raw personal token as the
complete `Authorization` value; Suyo therefore uses a trusted
`raw_authorization` auth mode and does not add a `Bearer` prefix.

## Safety policy

- Users cannot provide arbitrary MCP headers or select raw-header auth for a
  custom connector.
- Only tools in the built-in Tencent Docs allowlist are published to the
  agent. A direct call outside the allowlist is rejected again at execution.
- Only the fixed query/search/read set can run without an interactive
  permission decision. Every allowlisted create, append, add, or update tool
  requires approval and fails closed in non-interactive tasks.
- Delete tools are not published and are rejected if called directly.
- Connection failures, tool-call exceptions, and MCP `isError` text are
  scrubbed if an upstream response happens to echo the personal token.

## Verification

The mock contract suite is credential-free and should run in normal CI:

```bash
backend/.venv/bin/python -m pytest -q \
  backend/tests/test_mcp/test_tencent_docs_contract.py
```

Two optional real-server checks are available. Neither runs in normal CI and
neither should use a personal production document.

> A direct optional `pytest` invocation exits successfully when the test is
> skipped, so it is useful for development but is **not release evidence**.
> RC/GA evidence must be captured through
> `scripts/v1-real-integration-gates.mjs`, which fails closed on missing
> credentials, partial fixture configuration, skipped tests, or failed restore.

### Read-only discovery

The first check authenticates, connects to the official endpoint, runs
`tools/list`, verifies that `get_content` is present, and verifies that every
published tool passed Suyo's built-in allowlist:

```bash
TENCENT_DOCS_E2E_TOKEN='<personal token>' \
  backend/.venv/bin/python -m pytest -q \
  backend/tests/test_mcp/test_tencent_docs_contract.py \
  -k optional_real_server_lists
```

With no token, the check is safely skipped.

### Reversible write/read-back/restore

The write check is deliberately harder to enable. Create a dedicated test
document (or sheet/range) containing a stable baseline value such as
`SUYO_E2E_READY`. Use the current argument schema returned by the official
server's `tools/list`; Tencent may evolve these schemas independently of Suyo.

The following environment variables are all required after write access is
acknowledged:

| Variable | Purpose |
| --- | --- |
| `TENCENT_DOCS_E2E_TOKEN` | Personal token for the dedicated test account |
| `TENCENT_DOCS_E2E_ALLOW_WRITE` | Must exactly equal `I_UNDERSTAND_THIS_MODIFIES_A_DEDICATED_TEST_DOCUMENT` |
| `TENCENT_DOCS_E2E_TEST_DOCUMENT_ID` | ID of the dedicated document; the exact value must occur in every argument object |
| `TENCENT_DOCS_E2E_BASELINE_TEXT` | Stable text visible before the test and again after restore |
| `TENCENT_DOCS_E2E_READ_TOOL` | Optional; defaults to `get_content` |
| `TENCENT_DOCS_E2E_READ_ARGS_JSON` | JSON object for reading the dedicated fixture |
| `TENCENT_DOCS_E2E_WRITE_TOOL` | Allowlisted, approval-required update/append tool |
| `TENCENT_DOCS_E2E_WRITE_ARGS_JSON` | JSON object containing `{{SUYO_TENCENT_DOCS_E2E_MARKER}}` where the random marker is written |
| `TENCENT_DOCS_E2E_RESTORE_TOOL` | Allowlisted, approval-required tool that restores the fixture |
| `TENCENT_DOCS_E2E_RESTORE_ARGS_JSON` | JSON object containing `{{SUYO_TENCENT_DOCS_E2E_BASELINE}}` where the baseline is restored |

Run only the reversible check after injecting those values through a local
secret manager or CI secret store:

```bash
backend/.venv/bin/python -m pytest -q \
  backend/tests/test_mcp/test_tencent_docs_contract.py \
  -k optional_real_server_reversible_write_cycle
```

Before connecting, the harness rejects a partial opt-in, malformed JSON,
arguments that do not reference the dedicated document ID, or missing
write/restore placeholders. After discovery it also rejects tools that are
missing, disallowed, or not classified as requiring approval. The live cycle
then performs baseline read -> random-marker write -> read-back -> restore ->
baseline read-back. Restore runs from `finally`, including when the write has
an ambiguous transport failure. A restore failure is a hard test failure and
requires the operator to inspect the dedicated fixture manually.

Tencent Docs can add tools over time. Newly returned tools remain unavailable
until their names and risk classification are reviewed and added to
`backend/app/data/connectors.json`.

For release evidence, exact commands, freshness/commit binding, and recovery
procedures, see [v1.0 real-integration gates](releases/v1-real-integration-gates.md).
