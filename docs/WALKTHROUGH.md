# Five-minute workbench walkthrough

This walkthrough uses synthetic data and needs no provider API key. Start the local
Compose stack (`docker compose up --build --wait`), wait for its health checks, and open
`http://localhost:8000/`. An isolated development stack may publish a different loopback
port.

Every screen has the same top bar. Its three segments show who is asking (**Persona**),
the **Valuation date** and the knowledge cutoff (**Known as of**). Each segment opens one
shared context form. Nothing changes until you choose **Apply and start a fresh view**,
which clears the conversation, any open evidence and selections on the server. The menu
on the left lists only the screens your persona's role may use. Every page read and
every evidence open checks your current access again.

The timed path follows the welcome page's own tour: the recorded replay, then three
scenario presets, a discrepancy, an incomplete valuation and a denial. Every other
preset and screen is listed at the end as optional.

## 0:00: Follow a figure to its raw record

On the welcome page, choose **Take the five-minute tour**. This applies the **Clean
household** preset: `ADV-01`, household `WC-HH-0001`, valuation date 30 Sep 2026, known
as of 31 Oct 2026, 16:00 UTC. It opens the **Guided replay** page. Choose **Run replay**.

The recording holds one genuine question and the tool choices a live model made for it.
Replay runs those choices again through fresh retrieval, current policy and the answer
verifier. It does not redisplay recorded financial results, call a provider, or count
as an evaluation. The answer shows household assets of USD 3000.00: USD 2500.00 for
100 shares and USD 500.00 cash.

Under **Verified figures**, open the household assets figure. The evidence drawer opens
beside the answer and shows the claim, the governed result it resolved against, and the
result's source handle. **Source records** re-authorizes that handle and lists the
received feed records behind the result: source system, record ID, business date, the
time the platform received it, the feed run, a payload hash and the payload itself. The
list is capped honestly: the drawer says how many records it shows out of how many
(`item_count` and `total_item_count`) and offers no invented next page. **Trace of this
run**, under the answer, opens the run's trace. Close the drawer with **Close** or
Escape; focus returns to the figure.

## 1:15: Explain a settlement discrepancy

Choose **Scenarios** in the top bar, then **Open** on **Settlement and reconciliation**.
This applies `ADV-03`, household `WC-HH-0013`, valuation date 1 Oct 2026, known as of
2 Oct 2026, 12:00 UTC, and opens the household's **Reconciliation** tab.

The household is complete, with household assets of USD 22497.00. Two warnings are
listed with their subjects: `QUANTITY_BREAK` on `WC-AC-0030:WC-SEC-0001` and
`CASH_BREAK` on `WC-AC-0030:USD`. The table shows each account's trade-date figures from
portfolio accounting (PAS) beside custody's settled figures, the difference and the
status, all as the platform returned them. For `WC-AC-0030`, custody has not yet
received the settlement update at this cutoff. The business date has reached
settlement, so the platform calls it a break, not pending settlement.

Choose the **Known as of** segment, change it to `2026-10-02T16:00:00Z` and apply. The
fresh view opens on **My book**. Open `WC-HH-0013` and its **Reconciliation** tab again:
every row now matches and no flag is listed. The valuation date is the same; what
changed is what the platform knew.

## 2:30: Inspect an incomplete valuation

Return to the scenarios and open **Missing authoritative input**: `ADV-03`, household
`WC-HH-0003`, valuation date 15 Oct 2026, known as of 31 Oct 2026, 16:00 UTC.

The household page says **Valuation incomplete. No total is shown.** It shows the
**Fresh subtotal (not a total)**, USD 300.00, then the components left out of it: a
stale price for `WC-AC-0005:WC-SEC-0003` (last known USD 1120.00, price date 9 Oct 2026)
and account `WC-AC-0006`, whose authoritative snapshot was not received. Each warning
is listed with its subject. A missing account is not treated as a zero balance, and
custody data does not stand in for the missing portfolio-accounting snapshot.

Open **My book** from the menu. On this date a portfolio-accounting feed failed, so all
four of `ADV-03`'s households are incomplete. The concentration check lists them as
indeterminate, with no percentage, and the household list shows each one's fresh
subtotal in its own column. There is no book total on any day: the platform defines no
metric that adds households together.

## 3:30: See a current-access denial

Return to the scenarios and open **Current access**: `ADV-03` requests household
`WC-HH-0004` at 30 Sep 2026. The page says only **You are not authorized for the
requested resource**, with a trace reference for a compliance team. `ADV-03`'s coverage
of this household ended on 1 Oct. Access is checked at the time of the request, not on
the valuation date, so an earlier date does not restore it. The denial carries no
household data, and it reads the same for a household that does not exist.

Open the **Persona** segment, choose `ADV-05` and apply. `ADV-05`'s book lists
`WC-HH-0004` as a complete household. Nothing from `ADV-03`'s session carries over: the
earlier view, conversation and evidence links are gone.

## 4:30: Where to go next

That is the five-minute path. The optional stops below need no key either.

## Optional

- **Seeded revocation.** On the **Current access** denial, choose **Open the seeded
  revocation**. In a store of its own, a conversation from before the reassignment
  shows its earlier result as paired tombstones, and its source handle now gets the
  same neutral denial as one that never existed. **Run the recorded follow-up** replays
  a genuine recorded turn that is offered no data tool, so it abstains without a new
  retrieval.
- **Late trade (S09).** `COMP-01` sees `WC-HH-0009` at 30 Sep 2026 on the **Point in
  time** page, at two knowledge cutoffs side by side: each snapshot and the feed
  receipts behind it. No figure is differenced between the cutoffs.
- **Failed feed, seen by operations (S07).** `OPS-01` opens the **Feed run** page for the
  failed PAS-B snapshot feed of 15 Oct 2026: the accounts it left unavailable with
  their stored reason codes, and the households they belong to with their
  completeness, by identifier only. **Data health** shows every feed, source and
  business date as a grid with a glyph and a word in each cell, and as a table. No
  person name, household name or account title appears on either page.
- **Compliance.** `COMP-01`'s menu also has **Access decisions**, recorded authorization
  decisions with filters and a cursor, the counts as the platform returned them and the
  entitlement facts at each decision's stored revision, and **Trace**, which shows a
  trace's span tree from a closed list of span names, with no attributes.
- **Engineer.** `ENG-01`'s menu has **Metrics**, the governed metric definitions, and
  **Tools and schemas**, the seven MCP tools with their REST routes and published JSON
  Schemas. An engineer's menu and pages reach no trace page, span, household page,
  evidence or figure; see the departure below.
- **Ask.** With a configured provider key, **Ask** sends a new question through the
  live harness. Only a verified answer appears. The verifier's retry and fallback are
  labelled on the answer, and live calls incur provider usage.

## Boundaries

The persona selector is a loopback-only development convenience for known synthetic
identities. Its opaque cookie is HttpOnly, and form changes require the current
same-origin CSRF token. API and MCP requests still require their own bearer token; the
demo cookie does not authenticate them. Do not expose this development selector as
production authentication.

Only verified harness responses appear as answers. Prose from an external MCP host is
governed by host guidelines but is not verified by this workbench. Live answers can
incur provider usage and remain separate from scored evaluation. The scored results are
in the [README](../README.md#evaluation-results).

**Engineers and traces.** The design intent is that engineers see contracts
only. In the workbench that holds: the engineer's menu lists no trace page, and
`/api/trace_spans` refuses engineers. One accepted rule from the first release
stays: an engineer can still get trace metadata (results and authorization
decisions) from `POST /api/trace` for their own traces, and only for traces whose
recorded scope holds no household. The trace inspector route reads the same rule when
an engineer opens it by its URL. Removing the rule would change the accepted access policy.
See [limitations](LIMITATIONS.md).
