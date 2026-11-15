# Code standards

- Keep ingestion, authoritative calculations, authorization, transport and agent explanation separate.
- Use typed Pydantic contracts with unknown fields rejected. REST and MCP must call the same service layer and publish the same contract version.
- Use Decimal for financial arithmetic; serialize financial decimals as strings. Never derive authoritative values from a model's prose.
- Capture request time once. Use timezone-aware UTC instants and explicit business dates. Inject a clock into time-dependent behavior.
- Authorize before existence lookup and retrieval. Use a current entitlement snapshot even for historical valuation. A policy error denies access.
- Only the app touches SQLite and OPA. Put database writes inside the shared write transaction; do not carry database connections between threads.
- Keep modules focused. New endpoints must expose explicit failure states and test their boundary with the service/transport they depend on.
- Test plan scenarios and failure behavior, not just implementation details. Deterministic tests use synthetic data and no model/network service except the local containers explicitly required by integration checks. Live-model evaluation is a separate recorded P5 gate.
- Run `make check` for Python lint/types/tests. Run `make test` for containerized Python and policy tests.
- Application lint scope is `src`, `tests` and executable build scripts.
- Do not weaken thresholds or checks to get a pass. Diagnose failures and fix code or a demonstrably incorrect test.
