"""Public financial criteria are expressible through actual received-result contracts.

These are synthetic candidates, not provider evaluation scores. Authorization is
stubbed here; the separate real-OPA security suite owns authorization acceptance.
Expected values remain the independently reviewed YAML and oracle checks.
"""

from pathlib import Path

import pytest

from wealthcontext.bootstrap import bootstrap_history
from wealthcontext.clock import FrozenClock
from wealthcontext.context_service import ContextService
from wealthcontext.contracts import Answer
from wealthcontext.database import migrate
from wealthcontext.evaluation import grade_answer, load_cases
from wealthcontext.evaluation_contracts import GovernedResult
from wealthcontext.identity import Principal
from wealthcontext.policy import PolicyClient, PolicyDecision
from wealthcontext.storage import migrate_business
from wealthcontext.telemetry import create_provider
from wealthcontext.verifier import verify_answer

ACCEPTANCE = Path(__file__).parents[1] / "acceptance"
CASES = [case for case in load_cases(ACCEPTANCE) if case.category == "financial"]
REVISION = "sha256:" + "a" * 64


class FinancialContractPolicy(PolicyClient):
    """A financial-contract stub; no security assertion is made by this fixture."""

    def __init__(self):
        super().__init__("http://unused.invalid", REVISION)

    async def decide(self, principal, action, resources, facts, revision, request_business_date):
        return PolicyDecision(allow=True, allowed_ids=resources, projection="full",
                              reason="SYNTHETIC_CONTRACT_TEST", policy_revision=REVISION,
                              entitlement_revision=revision)

    async def filter_resources(self, principal, action, candidates, facts, revision,
                               request_business_date):
        return await self.decide(principal, action, candidates, facts, revision,
                                  request_business_date)


@pytest.fixture(scope="module")
def service(tmp_path_factory):
    directory = tmp_path_factory.mktemp("acceptance-contract")
    database = directory / "acceptance.sqlite"
    migrate(database)
    migrate_business(database)
    bootstrap_history(database, directory / "received", FrozenClock(CASES[0].request_time))
    telemetry = create_provider(directory / "traces.jsonl")
    yield ContextService(database, FinancialContractPolicy(), telemetry.get_tracer("acceptance"))
    telemetry.shutdown()


def resolve(value, results):
    if not isinstance(value, str) or not value.startswith("$"):
        return value
    alias, *parts = value[1:].split(".")
    current = results[alias]
    for part in parts:
        current = current[int(part)] if isinstance(current, list) else current[part]
    return current


@pytest.mark.anyio
@pytest.mark.parametrize("case", CASES, ids=lambda case: case.id)
async def test_public_financial_case_has_valid_resolved_answer_on_received_data(service, case):
    results, governed = {}, []
    for request in case.requests:
        arguments = {key: resolve(value, results) for key, value in request.arguments.items()}
        result = await service.execute(Principal(case.principal.id, case.principal.role),
                                       case.request_time, request.tool, arguments)
        results[request.alias] = result
        governed.append(GovernedResult(tool=request.tool, arguments=arguments, envelope=result))
    claims = [{
        "id": f"c{number}", "result_id": results[fact.request]["result_id"],
        "entity": {"type": fact.entity_type, "id": resolve(fact.entity_id, results)},
        "field": fact.field, "household_id": fact.household_id,
        "valuation_date": fact.valuation_date.isoformat(), "unit": fact.unit,
        "format": "label_and_value",
    } for number, fact in enumerate(case.required_facts, 1)]
    predicates = [{
        "id": f"p{number}", "result_id": results[item.request]["result_id"],
        "entity": {"type": item.entity_type, "id": resolve(item.entity_id, results)},
        "field": item.field, "kind": item.kind, "expect": item.expect,
    } for number, item in enumerate(case.required_predicates, 1)]
    cited = {item["result_id"] for item in [*claims, *predicates]}
    caveats = {(flag["code"], flag["subject"])
               for result in results.values() if result["result_id"] in cited
               for flag in result["quality_flags"] if flag["severity"] == "warning"}
    candidate = Answer.model_validate({
        "answer_markdown": "Evidence: " + ", ".join(
            "{{" + item["id"] + "}}" for item in [*claims, *predicates]
        ) + ". " + " ".join(case.required_mentions),
        "claims": claims, "predicates": predicates,
        "caveats": [{"flag": flag, "subject": subject} for flag, subject in sorted(caveats)],
        "refusal": None,
    })
    rendered = verify_answer(candidate, {
        result["result_id"]: result for result in results.values()
    })
    grade = grade_answer(case, rendered, governed)
    assert grade.passed, grade.diagnostics
