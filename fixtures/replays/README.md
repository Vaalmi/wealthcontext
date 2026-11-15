# Recorded live replay evidence

`clean-household.openai.json` is a genuine, unscored OpenAI calibration recording of
public case F01 using `gpt-6-luna` at reasoning effort high, the primary live model the
owner chose on 2026-11-08. It was captured on 2026-11-09 at 04:16:40–04:17:03 UTC as
trial 1 of F01 in the seventh GPT-6-Luna dry run (Unit C round 2), which was stopped
after F01 to take this recording. It contains synthetic
household data, authorized request/response pairs, actual provider response identities
and the verified terminal answer. It passed verification and the public F01 fact checks
on the first attempt, without answer retry, transport retry or fallback. This single
calibration is not the three-trial primary-model acceptance gate.

The fixture is the exact recorded run (`replay.save_recording`), with no model response
edits. Its file SHA-256 is
`bd5233a27b7cd3434686399d54e64085269deeeb1276968f729cf2554251ae07`;
its canonical payload SHA-256 is
`52e50841614ca7c7722c4c676af725c3e0e86148f39a5a540674192fc2b8ba5f`.
The run used prompt SHA-256
`67ec96d02ef614d80860bae4f7b4b6ce012164a86439f79ec7bf21f6064943a6`, the harness prompt
committed with it. The recording contains no provider credentials and passed a gitleaks
scan. The model's qualitative prose remains subject to the documented verifier limits.

It replaces four earlier Anthropic `claude-opus-5-5` recordings of the same case, all
still in Git history as `clean-household.anthropic.json`:

- the 2026-11-02 recording (payload SHA-256
  `e865071fe3c5104e4f32dac5e09e0b02c44b5afed47a3d6d12e59f2349a994ac`);
- three 2026-11-05 successors (payload SHA-256
  `a1ef8c4b0e12966c935c12565c80f68685e48004ac2630a867d6159f566a0200`,
  `04191dd4d76f555c6c780f7cbe2578230295700e4fd27c4ad31038e8b9e44a32` and
  `b96f116271c025e589afbe9ff2a97836693a7c61410175a73df914240cbe7465`).

Harness prompt changes invalidated each of them, because replay requires the exact
recorded system prompt; the 2026-11-05 interim recording was also invalidated when the
run record gained per-turn transport retry counts. The last one was replaced when the
owner made GPT-6-Luna the primary live model and the prompt gained generic binding
guidance (ADR 0008, 2026-11-08 amendment).

Replay re-executes the recorded tool choices against fresh received data and current
authorization. It does not serve saved financial responses or call a provider.
The exact original question, principal, prompt and tool contracts are required, and
the fresh governed results must agree with the recorded ones apart from paired
identifiers, run metadata and placeholder-rendered numeric values; changed evidence or
revoked access fails closed. A recording that no longer matches the active contract
must be regenerated from a genuine live run, not edited to look compatible or
relabeled as live evidence.

## A7 follow-up: `revocation-followup.anthropic.json`

The seeded revocation page (A7, build plan decision 12) replays one genuine recorded
turn: the follow-up that security case Q02 asks after the reassignment of WC-HH-0004.
The owner chose on 2026-11-10 to take it from an archived scored trial rather than record
a new one, so no new API call was made. It is trial 1 of Q02 in the Anthropic
`claude-opus-5-5` scored run `eval_c56d7beb5729403bb2851041466afa38` of 2026-11-09 (mode
`live_scored`, harness run `run_b39ce536496b4a62aedb9dbecc267442`, captured
04:49:50–04:49:54 UTC), whose stored trial payload SHA-256 is
`3381677cf7a993575537ca9fee84c640a56ba42ce09ae9ed31932df74d828f8d`. Only the trial's
transcript, the exact `HarnessRun`, was saved through `replay.save_recording`, with no
edit; no grade or rubric was copied. Its file SHA-256 is
`02ab21831b4a5995e442b2b9f2f837c4afb96f33b4e532f2a11f630139a66260`; its canonical
payload SHA-256 is
`f954403466972a5f5d4e1cad3cb34384dce0a8826518a475de2f71a5e2abc3d1`. It used the
current harness prompt (SHA-256 `67ec96d0…a943a6`), passed verification on the first
attempt, made no data tool call (none was offered), and used no answer retry, transport
retry or fallback. It passed a gitleaks scan.

Its opening request holds the staged earlier turn only as paired authorization
tombstones: two calls with no arguments, their tombstoned results and the tombstoned
earlier answer, with no authorized result. Replay accepts such an opening and nothing
else before the first recorded turn (`replay.prior_tombstones`), and refuses the turn if
any of those calls is no longer a tombstone. Its answer is a qualitative abstention with
no claim, predicate or refusal; as the Q02 limitations note, that prose is not certified
beyond the verifier's checks.
