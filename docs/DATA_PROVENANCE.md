# Dataset and evidence provenance

The demonstration uses generated synthetic records. It does not ingest a client,
custodian or market-data provider's real records. Fictional names, account numbers,
security identifiers, prices, FX observations and transactions exist to exercise the
approved scenarios. Identifier checksums and formats do not make these real securities.

`src/wealthcontext/generator.py` produces a seeded history of CRM, custody, portfolio
accounting, security-master and FX deliveries. The default seed is 2026. Exported
files, their manifests and the receipt index preserve failed fetches, incomplete
accounts, truncated bytes, late receipts and corrections. They contain received
evidence, not an expected-answer file. Canonical facts retain source identities and
business-effective intervals; importing the history later does not rewrite simulated
receipt times.

The three times serve different purposes: business-effective dates describe when a
fact applies; recorded receipt times determine what was known at a cutoff; import and
calculation times describe execution. Current authorization is checked even when a
query asks for historical facts.

The platform calculates financial figures from accepted received records using
Decimal arithmetic and versioned metric definitions. The independent
`reference_oracle` package reads exported files and manifests without importing the
production implementation. Its hand-authored fixtures and shared received-file
comparisons check arithmetic, ambiguity, freshness and historical corrections.

The public `acceptance` directory declares financial and security cases. Expected
facts are grader inputs only and are excluded from model requests. Their exact hash
must be committed before a scored run. A generated dataset, a replay and an unscored
calibration do not establish the model's scored accuracy.

`fixtures/replays/clean-household.openai.json` preserves an actual, successful,
unscored GPT-6-Luna response over the synthetic clean-household scenario. Replay
reexecutes its tool choices against fresh governed results and current permissions;
it does not redisplay old stored financial envelopes. No provider key is needed.
The recording is tied to its original persona, question, dates, prompt and schema.

Provenance handles resolve through the same authorization layer as other reads.
Returned source records can be projected or redacted to the caller's permitted scope.
A raw-record hash identifies the original received bytes; it is not a claim that a
projected response contains those complete bytes. Tool envelopes, audit decisions and
trace IDs remain distinct from an external host's unverified explanation.
