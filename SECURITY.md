# Security policy

## What this project is

WealthContext is a local demonstration that runs on synthetic data. Its development
identities, JWT keys and browser persona selector are conveniences for a loopback-only
demo. They are not production authentication, and the project is not meant to be exposed
to a network or run against real client data. The
[limitations](docs/LIMITATIONS.md) describe the boundary.

Within that boundary, the access controls are the point of the project, and a way around
them is a real bug.

## Reporting a vulnerability

Please report privately, not in a public issue or pull request. Use **Report a
vulnerability** on the repository's
[Security tab](https://github.com/Vaalmi/wealthcontext/security/advisories/new), which
opens a private advisory visible only to the maintainers. If you can't use that, write to
**contact@vaalmi.com**.

Include what you did, what you expected and what happened, with the persona, dates and
request if you can. We will acknowledge the report, tell you what we find, and credit you
in the fix if you would like. This is a small project, so responses are best effort.

## In scope

- Reading a household's data, a provenance record or a trace without current access,
  through REST, MCP, the harness or the workbench.
- Data that stays reachable after access is revoked: reused handles, earlier results or
  earlier answers.
- A denial that reveals whether a household exists.
- Names or account titles reaching a role whose projection removes them.
- A figure shown as verified that does not resolve to an authorized governed result, or
  a verified answer that drops a warning.
- A real secret, credential or real personal data anywhere in the repository or its
  history.

## Out of scope

- The persona selector letting anyone on the local machine choose a synthetic persona.
  That is how the demo works.
- The development signing keys and tokens generated for a local install.
- Exposing the demo to a network against this guidance.
- What an external MCP host writes in its own prose. WealthContext does not verify it,
  and says so.

## Supported versions

Fixes are made on `main`.
