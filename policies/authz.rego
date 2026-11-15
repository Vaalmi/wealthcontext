package wealthcontext.authz

import rego.v1

known_role if input.principal.role in {"advisor", "ops", "compliance", "engineer"}

active(fact) if {
    fact.valid_from <= input.request_business_date
    input.request_business_date < fact.valid_to
}

covered(resource) if {
    input.principal.role == "advisor"
    some fact in input.facts
    fact.kind == "advisor_covers"
    fact.subject == input.principal.id
    fact.object == resource
    active(fact)
}

covered(resource) if {
    input.principal.role == "advisor"
    some membership in input.facts
    membership.kind == "advisor_member"
    membership.subject == input.principal.id
    active(membership)
    some coverage in input.facts
    coverage.kind == "team_covers"
    coverage.subject == membership.object
    coverage.object == resource
    active(coverage)
}

covered(resource) if {
    input.principal.role == "compliance"
    resource in input.resources
}

filtered if {
    input.action == "concentration_book"
}

filtered if {
    input.action == "search_households"
    object.get(input, "resource_mode", "exact") == "filter"
}

default allow := false

allow if {
    known_role
    input.action in {"metric_definition", "result_preflight", "conversation_preflight",
                    "trace_preflight", "error_result"}
    count(input.resources) == 0
}

allow if {
    input.principal.role in {"advisor", "compliance"}
    input.action in {"concentration_book_preflight", "search_preflight"}
    count(input.resources) == 0
}

allow if {
    input.principal.role in {"advisor", "ops", "compliance"}
    input.action == "provenance_preflight"
    count(input.resources) == 0
}

allow if {
    input.principal.role in {"ops", "compliance"}
    input.action == "quality_preflight"
    count(input.resources) == 0
}

# ADR 0009 section 6: receipts are compliance only, exact, one household.
allow if {
    input.principal.role == "compliance"
    input.action == "household_receipts_preflight"
    count(input.resources) == 0
}

allow if {
    input.principal.role == "compliance"
    input.action == "household_receipts"
    count(input.resources) == 1
}

# ADR 0009 section 7: spans are refused to engineers and operations at the preflight; an
# advisor must cover every household of the trace (ownership is checked by the service).
allow if {
    input.principal.role in {"advisor", "compliance"}
    input.action == "trace_spans_preflight"
    count(input.resources) == 0
}

allow if {
    input.principal.role in {"advisor", "compliance"}
    input.action == "trace_spans"
    every resource in input.resources { covered(resource) }
}

allow if {
    input.principal.role in {"advisor", "compliance"}
    filtered
}

allow if {
    input.principal.role in {"advisor", "compliance"}
    input.action in {"concentrated_positions", "verify_claim",
                    "search_households", "provenance", "trace_scope",
                    "conversation_context"}
    not filtered
    every resource in input.resources { covered(resource) }
}

allow if {
    input.principal.role in {"advisor", "compliance"}
    input.action in {"snapshot", "portfolio_exposure", "view_conflicts"}
    count(input.resources) == 1
    every resource in input.resources { covered(resource) }
}

allow if {
    input.principal.role == "ops"
    input.action in {"view_quality", "feed_runs", "provenance", "trace_scope",
                    "conversation_context", "verify_claim"}
}

allow if {
    input.principal.role == "ops"
    input.action == "view_conflicts"
    count(input.resources) == 1
}

allow if {
    input.principal.role == "compliance"
    input.action in {"view_quality", "feed_runs", "view_decision_log", "view_eval_reports"}
}

allow if {
    input.principal.role == "engineer"
    input.action in {"trace_scope", "conversation_context", "verify_claim"}
    count(input.resources) == 0
}

allowed_ids := sort([resource | some resource in input.resources; covered(resource)]) if {
    allow
    filtered
} else := input.resources if allow
else := []

projection := "no_pii" if {
    input.principal.role == "ops"
    input.action != "metric_definition"
} else := "full"

reason := "CURRENT_GRANT" if allow
else := "NO_CURRENT_GRANT"

# Cite the bundle which actually supplied this decision; entitlements remain request input.
revisions := {bundle.manifest.revision | some bundle in data.system.bundles}

decision := {
    "allow": allow,
    "allowed_ids": allowed_ids,
    "projection": projection,
    "reason": reason,
    "policy_revision": revision,
    "entitlement_revision": input.revision,
} if {
    count(data.system.bundles) == 1
    some revision in revisions
}
