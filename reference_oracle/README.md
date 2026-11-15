# Independent received-feed reference oracle

This standard-library-only package implements independent P3 financial reference calculations.
It reads `index.json`, manifests and the exact received JSONL files. It imports no production
code, uses no database or generator internals, and never reads latent-world expected answers.

From the repository root:

```sh
python -B -m unittest discover -s tests -p test_reference_oracle.py
python -B -m reference_oracle /path/to/received-export \
  --household WC-HH-0010 --as-of 2026-10-08 \
  --known-as-of 2026-10-31T23:00:00Z --summary
python -B -m reference_oracle /path/to/received-export \
  --known-as-of 2026-10-31T23:00:00Z --validate-only
python -B -m reference_oracle /path/to/received-export \
  --all-households --as-of 2026-09-30 --threshold-pct 10.00 \
  --known-as-of 2026-10-31T23:00:00Z --summary
uv run pytest tests/test_reference_oracle.py tests/test_p3_oracle_agreement.py
```

`load_received_history(directory)` validates received evidence.
`evaluate(history, household_ids=[...], as_of=date, known_as_of=aware_datetime,
threshold_pct=Decimal('10.00'))` returns financial facts and diagnostics. The explicit household
scope is a calculation input, not an authorization decision. The CLI emits JSON and rejects
invalid inputs with exit status 2. Omit `--summary` for component and raw-record evidence.

The separate reader checks file/manifest hashes, header identity, newline framing and
finance-relevant schemas. Snapshot account groups require full declared counts/digests;
malformed or truncated accounts never publish partially. Valid zero-row accounts remain
distinct from unavailable accounts. Raw external-account publications survive missing CRM at
arrival and bind to the current unambiguous crosswalk at both query cutoffs. Original manifest
account labels remain separately identified in output evidence.

Temporal replay preserves receipt times, immutable raw-record identity and subsequent
non-snapshot assertion times. Tied applicability conflicts remain unresolved; both-active
identical values can agree despite different future ends. Corrections never rewrite older
knowledge cutoffs. Prices select the newest eligible close before corrections at that close.
Non-snapshot assertion comparison fills declared semantic defaults such as unknown ISIN;
the raw records remain unchanged. Snapshot ties instead require the same exact raw record
set (source, source record ID, file date and raw payload hash), independent of row order.
Different record identities or validity bounds cannot be merged merely because balances
match. Explicit unavailable publications remain unavailable when only their failure reasons
differ; their shared diagnostic is `MULTIPLE_UNAVAILABLE_PUBLICATIONS`.
Current-day foreign fixes require both the file date and UTC fix timestamp date to match the
valuation date; no FX carry-forward is allowed. Source timestamps cannot exceed the knowledge
cutoff. File date is part of FX assertion equivalence before tied observations collapse:
same-time fixes from different file dates are ambiguous in either receipt order. USD is the
exact conversion identity.

Calculations use Decimal precision 28 / half-even intermediates. Positions and cash are
each sorted by component identifier and accumulated separately from Decimal zero before
the two subtotals are added. Issuer, fund and security aggregation follows the same sorted
position order. Thresholds are compared before rounding; money and percentages use half-up
output quantization. The stage order matters at the accepted 28-digit magnitude boundary.
The fixed calendar is the plan's 25-weekday window, with no foreign holiday model. Custody
prices remain authoritative when optional closed PAS-price observations disagree. Informational
`PRICE_SOURCE_MISMATCH` details retain both raw records and reasons `CURRENCY`, `CLOSE_DATE`,
and `PRICE` only when comparable. Missing or stale custody never falls back to PAS pricing.

The result contains separately ranked `issuer_exposures`, `fund_vehicle_exposures` and
`security_exposures` objects, with scopes `household_issuers`, `household_fund_vehicles` and
`household_securities`. Issuers use `concentration_pct`; fund/security items use `allocation_pct`.
Incomplete results omit these lists; nonpositive totals omit percentage keys. Each list has
returned/full counts, ordering, truncation and null cursor metadata. Their top-20 limits apply
after full valuation, aggregation and comparison. Concentration breaches distinguish
`book_rank` from `issuer_rank`; every indeterminate household is retained without a cap.
The CLI's `--all-households` scope includes unresolved household assertions, which remain
explicitly indeterminate. Component security and price ambiguities use `AMBIGUOUS`; missing
evidence uses `SOURCE_UNAVAILABLE`. Both inputs are inspected even when one is unavailable.

Hand-authored tests construct received files using their own `json`/`hashlib` writer and assert
manual financial results. An isolated `-I -S` subprocess forbids production imports. Agreement
tests may import both implementations, but compare semantic facts rather than opaque handles
or IDs. The all-days agreement suite exports the public generator's received files, discards
its in-memory event list, and lets each implementation re-read the export. It compares all 14
households across 25 business dates, both exposure groupings, book concentration, FX and price
observations, completeness and relevant flags. Separate manual assertions cover S03/S10/S15,
B1/B2, PAS-price disagreement and S09 immediately before/at its receipt cutoff after a far-future
database bootstrap. Tests distinguish 384 indexed events, 383 present-file events and 382
distinct received contents rather than conflating events with content identity.

This oracle is a financial reference, not an authorization service or a full production clone.
It intentionally does not duplicate every ingestion schema validator, authorization rule,
reconciliation warning or provenance projection. In particular, finance-relevant validation
does not validate every title, identifier, transaction or lot constraint. The independent
reader checks enough received evidence to admit or withhold financial components, and output
lineage identifies raw files and records rather than application provenance handles.
