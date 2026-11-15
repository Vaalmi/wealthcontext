"""OPA connectivity and revision verification. Failure never creates an allow."""

from datetime import date

import httpx2
from pydantic import Field, StrictBool

from wealthcontext.contracts import Contract, Identifier, PolicyRevision
from wealthcontext.identity import Principal


class PolicyDecision(Contract):
    allow: StrictBool
    allowed_ids: list[Identifier]
    projection: str
    reason: Identifier
    policy_revision: PolicyRevision
    entitlement_revision: int = Field(ge=0)


class PolicyUnavailable(RuntimeError):
    pass


class PolicyClient:
    def __init__(self, url: str, expected_revision: str):
        self.url = url.rstrip("/")
        self.expected_revision = expected_revision

    async def decide(
        self, principal: Principal, action: str, resources: list[str], facts: list[dict],
        revision: int, request_business_date: date,
    ) -> PolicyDecision:
        return await self._decide(principal, action, resources, facts, revision,
                                  request_business_date, filtered=False)

    async def filter_candidates(
        self, principal: Principal, candidates: list[str], facts: list[dict],
        revision: int, request_business_date: date,
    ) -> PolicyDecision:
        return await self.filter_resources(principal, "concentration_book", candidates, facts,
                                           revision, request_business_date)

    async def filter_resources(
        self, principal: Principal, action: str, candidates: list[str], facts: list[dict],
        revision: int, request_business_date: date,
    ) -> PolicyDecision:
        return await self._decide(principal, action, candidates, facts, revision,
                                  request_business_date, filtered=True)

    async def _decide(
        self, principal: Principal, action: str, resources: list[str], facts: list[dict],
        revision: int, request_business_date: date, *, filtered: bool,
    ) -> PolicyDecision:
        if len(resources) != len(set(resources)) or (
            filtered and action not in {"concentration_book", "search_households"}
        ):
            raise PolicyUnavailable("invalid authorization candidates or decision mode")
        payload = {"input": {
            "principal": {"id": principal.id, "role": principal.role}, "action": action,
            "resources": resources, "facts": facts, "revision": revision,
            "request_business_date": request_business_date.isoformat(),
            "resource_mode": "filter" if filtered else "exact",
        }}
        try:
            async with httpx2.AsyncClient(timeout=3.0, trust_env=False) as client:
                response = await client.post(
                    f"{self.url}/v1/data/wealthcontext/authz/decision", json=payload,
                )
            if response.status_code != 200:
                raise PolicyUnavailable("OPA decision could not be verified")
            decision = PolicyDecision.model_validate(response.json()["result"])
            expected_projection = ("no_pii" if principal.role == "ops"
                                   and action != "metric_definition" else "full")
            if (decision.policy_revision != self.expected_revision
                    or decision.entitlement_revision != revision
                    or decision.projection != expected_projection
                    or len(decision.allowed_ids) != len(set(decision.allowed_ids))
                    or not set(decision.allowed_ids).issubset(resources)
                    or (decision.allow and not filtered
                        and set(decision.allowed_ids) != set(resources))
                    or (not decision.allow and decision.allowed_ids)):
                raise PolicyUnavailable("invalid policy decision")
            return decision.model_copy(update={"allowed_ids": sorted(decision.allowed_ids)})
        except (httpx2.HTTPError, KeyError, TypeError, ValueError) as exc:
            raise PolicyUnavailable("OPA decision could not be verified") from exc

    async def verify_revision(self) -> str:
        try:
            async with httpx2.AsyncClient(timeout=3.0, trust_env=False) as client:
                response = await client.get(f"{self.url}/v1/data/system/bundles")
            if response.status_code != 200:
                raise PolicyUnavailable("OPA bundle could not be verified")
            bundles = response.json()["result"]
            revisions = {entry["manifest"]["revision"] for entry in bundles.values()}
            if len(bundles) != 1 or revisions != {self.expected_revision}:
                raise PolicyUnavailable("loaded policy revision does not match expected bundle")
            return self.expected_revision
        except (httpx2.HTTPError, KeyError, TypeError, AttributeError, ValueError) as exc:
            raise PolicyUnavailable("OPA bundle could not be verified") from exc
