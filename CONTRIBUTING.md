# Contributing to WealthContext

Thank you for looking. WealthContext is a reference implementation of a governed context
layer for wealth-management data. Bug reports, questions, documentation fixes and code
changes are all welcome.

## Before you start

- **Synthetic data only.** Never add real client, custodian or market-data records, real
  names or account numbers, credentials, or provider keys, in code, tests, fixtures,
  issues or pull requests. Everything in this repository is generated.
- **For anything larger than a small fix, open an issue first.** It saves you work if
  the change touches one of the areas below.
- **Security problems go through [SECURITY.md](SECURITY.md)**, not a public issue.
- Everyone taking part follows the [code of conduct](CODE_OF_CONDUCT.md). For anything
  else, the maintainers are at contact@vaalmi.com.

## Set up

You need Docker with Compose v2, and for host checks Python 3.12 and
[uv](https://docs.astral.sh/uv/).

```sh
uv sync --frozen
make up       # build and start the app and the policy engine
make check    # lint, types and host tests
make test     # rebuild, then run the deterministic and policy suites in containers
```

For a change to the workbench, also run the browser smoke:

```sh
uv sync --frozen --group browser
make browser-check
```

No provider API key is needed for any of these. CI runs the same checks from a fresh
checkout and takes 40 to 60 minutes or more, so run `make check` locally first.

## What a good change looks like

The [code standards](docs/CODE_STANDARDS.md) are short and worth reading in full. The
rules that matter most:

- The platform owns facts, permissions and arithmetic. Models interpret and explain. No
  authoritative value is ever derived from a model's prose.
- Financial arithmetic uses `Decimal`, and financial decimals are serialized as strings.
- Authorize before any existence lookup or retrieval, with current entitlements, even
  for a historical date. A policy error denies access.
- REST and MCP call the same service layer and publish the same contract.
- Test the failure and boundary behaviour, not only the happy path.
- Don't weaken a threshold or a check to get a pass. Fix the code, or show that the test
  is wrong.

## Changes that need a conversation first

Some files are inputs to the published evaluation results and to the accepted design. A
change to any of them means the results in the README no longer describe the code until
the scored evaluation is run again, so please open an issue before working on:

- `acceptance/` and `prompts/`
- `policies/` and `schemas/v1/`
- valuation, reconciliation and verifier behaviour in `src/wealthcontext/`
- `pyproject.toml` and `uv.lock`

## Pull requests

- Keep a pull request to one change, and say what you ran.
- Add or update tests with the change.
- Update the docs a reader would need: the [reference](docs/REFERENCE.md), the
  [walkthrough](docs/WALKTHROUGH.md) or [limitations](docs/LIMITATIONS.md).
- A maintainer reviews every pull request, and CI must pass.

AI-assisted contributions are welcome. You are responsible for what you submit, including
that you have the right to contribute it.

## License

WealthContext is licensed under [Apache-2.0](LICENSE). By submitting a contribution you
agree that it is licensed under the same terms, as section 5 of the license describes.
