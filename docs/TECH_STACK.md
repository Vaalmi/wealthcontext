# Technology choices

This file records the verified implementation choices.

| Concern | Choice |
| --- | --- |
| Runtime | Python 3.12, managed by uv; exact resolution in `uv.lock` |
| API and contracts | FastAPI 0.162.0 and Pydantic 2.14.2 |
| Agent interface | Official MCP Python SDK 2.3.0 (`MCPServer`, `Client`); HTTPX2 transport |
| Persistence | SQLite, WAL, foreign keys, 5-second busy timeout, one app write lock |
| Authorization | OPA 1.22.0 in a separate private-network container; Rego v1 |
| Demo identity | PyJWT 2.15.1 with cryptography; RSA dev keys and injectable-clock validation |
| Telemetry | OpenTelemetry SDK 1.45.1; semantic conventions 0.66b1; local JSONL exporter |
| Packaging | One app image, reused by the optional stdio-bridge service; Docker Compose |
| Workbench | Server-rendered HTML and small JavaScript modules, implemented in P6 |
| Deterministic checks | pytest, Hypothesis, JSON Schema, `opa test`; Playwright added for browser flows |

Dockerfile base images are pinned by immutable manifest digest, including Python, uv and official OPA. P0's app image includes test tools so `make test` runs in the same environment as the app. This is a local demo, not a production-minimal image.

Verified references (2026-11-01):

- [MCP 2.3.0 release](https://github.com/modelcontextprotocol/python-sdk/releases/tag/v2.3.0): official v2 API and HTTPX2 dependency.
- [MCP deployment guidance](https://py.sdk.modelcontextprotocol.io/run/deploy/): explicit Host/Origin allowlists for a mounted app. Exact `/mcp` integration is proved in P1.
- [OPA bundle format](https://www.openpolicyagent.org/docs/management-bundles): `.manifest` revision and bundle build.
- [OPA REST API](https://www.openpolicyagent.org/docs/rest-api): policy evaluation and health endpoints. The running pinned image exposes loaded revisions under `data.system.bundles[*].manifest.revision`; integration testing verifies that shape.
- [PyJWT usage](https://pyjwt.readthedocs.io/en/latest/usage.html): explicit signature/issuer/audience validation; application-owned temporal validation uses the captured clock.
- [OpenTelemetry SDK package](https://pypi.org/project/opentelemetry-sdk/1.45.1/): the pinned SDK and its matching API/semantic-conventions dependencies.
