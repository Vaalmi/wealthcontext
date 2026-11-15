# MVP boundaries and limitations

This is a local synthetic-data demonstration. Its development identities, JWT setup
and browser persona selector are not production authentication or multi-tenancy.
The repository makes no regulatory compliance claim and is not a financial-advice
product.

Financial scope excludes individual bonds and accrued interest, derivatives,
leverage/margin, negative total cash, liabilities, fund look-through, performance
returns, unrealized gains and losses, tax optimization and corporate actions.
Fund vehicles remain distinct from operating-company issuer exposures. Held-away
accounts are excluded rather than valued from invented records. Freshness uses the
declared synthetic weekday rules, not complete foreign-market holiday calendars.

Missing authoritative inputs withhold totals and percentages. Fresh subtotals and
last-known observations remain explicitly labeled. A missing secondary reconciliation
source can coexist with a complete authoritative valuation; it must remain disclosed.

The verified harness resolves numerical claims and predicates against fresh authorized
results, requires warning disclosure and checks a closed vocabulary of numerical and
comparative wording. Qualitative prose outside that vocabulary is not verified.
External MCP hosts receive guidelines, but their prose never becomes a verified
WealthContext answer merely by following them.

Two genuine recordings are bundled: the clean-household question and the seeded
revocation's follow-up turn. Replay is a repeatable demonstration, not a new provider
call or model evaluation. Every other preset opens a governed data page that needs no
key; a new question on any of them is a live answer, which requires a configured
provider and incurs API usage.

The workbench UX revamp keeps three limits on purpose:

- **Engineers and traces.** The design intent is that engineers see
  contracts only. The workbench menu lists no trace page for an engineer, and
  `/api/trace_spans` refuses engineers. The MVP's accepted `/api/trace` rule stays,
  so an engineer can still read trace metadata (results and authorization decisions)
  for their own traces whose recorded scope holds no household, through `POST
  /api/trace` or the trace inspector's URL. Removing it would change the accepted access
  policy. See [the walkthrough](WALKTHROUGH.md).
- **Evidence beyond 20 records.** The evidence drawer shows the first 20 source records
  of a result and says how many there are; a figure's own record may be among the rest.
  Lifting the cap would change `get_data_provenance`, an accepted MCP tool.
- **Deferred screens.** The operations reconciliation screen, the compliance view of a
  recorded response, household members and ownership, an operations conflict register
  and dedicated failure-state screens are not built; the restyled error page covers an
  expired session and an invalid date. There is no dark theme.

Live calls default to OpenAI GPT-6-Luna (`gpt-6-luna`, reasoning effort high), by the
maintainer's decision of 2026-11-08. On 2026-11-09 the maintainer made Anthropic `claude-opus-5-5`
the primary model for the scored acceptance gate, which it met on the MVP's evaluation
inputs (20 of 20); GPT-6-Luna was the published GPT comparison (17 of 20). The workbench
revamp changed those inputs, so the scored evaluation ran again before the revamp was accepted. Over
those runs the maintainer named Claude Sonnet 5.5, then OpenAI GPT-6.1 Sol (at reasoning
effort high, then xhigh), then GPT-6.1 Terra (`gpt-6.1-terra`, reasoning effort high) as
the gate model, each after the one before had missed the gate. On the final evaluation inputs
(`d23a96e`; the run was on `a7697c4`, whose inputs are the same) GPT-6.1 Terra passed
20 of 20, so the primary-model gate is met, by the maintainer's adjudication of 2026-11-14: the
Terra run repeated scoring on unchanged inputs without a technical failure, which the
project's own review rules otherwise forbid. GPT-6.1 Sol at xhigh and GPT-6-Luna each
passed 17 of 20 there. The gate model was chosen after earlier models had missed, and
every scored run is listed in the [README](../README.md#evaluation-results). Every miss was a model answer that failed grading,
verification or citation authorization, not a provider failure. The Google SDK adapter
remains supported, but its live comparison is deferred and must be labeled not run. A first-attempt calibration success is not the primary-model acceptance gate:
all three scored trials must pass for every one of the twenty financial questions.
See the [README](../README.md#evaluation-results) for every scored run.
