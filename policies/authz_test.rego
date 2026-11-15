package wealthcontext.authz_test

import rego.v1
import data.wealthcontext.authz

request := {
    "principal": {"id": "ADV-01", "role": "advisor"},
    "action": "snapshot", "resources": ["WC-HH-0001"], "revision": 1,
    "request_business_date": "2026-10-01",
    "facts": [{"kind": "advisor_covers", "subject": "ADV-01", "object": "WC-HH-0001",
               "valid_from": "2026-10-01", "valid_to": "2026-10-10"}],
}

test_direct_current_grant if { authz.allow with input as request }
test_unknown_action_denied if {
    not authz.allow with input as object.union(request, {"action": "invented"})
}
test_unknown_household_denied if {
    not authz.allow with input as object.union(request, {"resources": ["unknown"]})
}
test_uncovered_advisor_denied if {
    not authz.allow with input as object.union(request, {
        "principal": {"id": "ADV-02", "role": "advisor"},
    })
}
test_future_grant_denied if {
    not authz.allow with input as object.union(request, {"request_business_date": "2026-09-30"})
}
test_half_open_grant_denied if {
    not authz.allow with input as object.union(request, {"request_business_date": "2026-10-10"})
}
test_entitlements_are_input_not_static_policy_data if {
    not authz.allow with input as object.union(request, {"facts": []})
}
test_decision_cites_actual_bundle_revision if {
    result := authz.decision with input as request with data.system.bundles as {
        "bundle": {"manifest": {"revision": "sha256:test"}},
    }
    result.allow
    result.policy_revision == "sha256:test"
    result.entitlement_revision == 1
    result.allowed_ids == ["WC-HH-0001"]
}

test_engineer_lineage_preflight_denied if {
    not authz.allow with input as object.union(request, {
        "principal": {"id": "ENG-01", "role": "engineer"},
        "action": "provenance_preflight", "resources": [],
    })
}

test_known_role_metadata_preflights_allowed if {
    every role in ["advisor", "ops", "compliance", "engineer"] {
        every action in ["result_preflight", "conversation_preflight", "trace_preflight",
                         "error_result", "verify_claim"] {
            authz.allow with input as object.union(request, {
                "principal": {"id": "TEST-01", "role": role},
                "action": action, "resources": [],
            })
        }
    }
}

test_neutral_errors_and_engineer_verification_never_authorize_household_scopes if {
    every role in ["advisor", "ops", "compliance", "engineer"] {
        not authz.allow with input as object.union(request, {
            "principal": {"id": "TEST-01", "role": role},
            "action": "error_result", "resources": ["WC-HH-0001"],
        })
    }
    not authz.allow with input as object.union(request, {
        "principal": {"id": "ENG-01", "role": "engineer"},
        "action": "verify_claim", "resources": ["WC-HH-0001"],
    })
}

test_lineage_preflight_roles if {
    every role in ["advisor", "ops", "compliance"] {
        authz.allow with input as object.union(request, {
            "principal": {"id": "TEST-01", "role": role},
            "action": "provenance_preflight", "resources": [],
        })
    }
}

team_facts := [
    {"kind": "advisor_member", "subject": "ADV-01", "object": "TEAM-1",
     "valid_from": "2026-09-14", "valid_to": "2026-10-10"},
    {"kind": "team_covers", "subject": "TEAM-1", "object": "WC-HH-0002",
     "valid_from": "2026-10-01", "valid_to": "2026-11-01"},
]

test_team_only_financial_access if {
    every action in ["portfolio_exposure", "concentrated_positions"] {
        authz.allow with input as object.union(request, {
            "action": action, "resources": ["WC-HH-0002"], "facts": team_facts,
        })
    }
}

test_team_membership_and_coverage_both_current if {
    every day in ["2026-09-30", "2026-10-10", "2026-11-01"] {
        not authz.allow with input as object.union(request, {
            "action": "portfolio_exposure", "resources": ["WC-HH-0002"],
            "facts": team_facts, "request_business_date": day,
        })
    }
}

test_book_returns_only_direct_and_team_authorized_candidates if {
    result := authz.decision with input as object.union(request, {
        "action": "concentration_book",
        "resources": ["WC-HH-0003", "WC-HH-0002", "WC-HH-0001"],
        "facts": array.concat(request.facts, team_facts),
    }) with data.system.bundles as {"bundle": {"manifest": {"revision": "sha256:test"}}}
    result.allow
    result.allowed_ids == ["WC-HH-0001", "WC-HH-0002"]
}

test_book_role_permission_can_have_empty_authorized_subset if {
    result := authz.decision with input as object.union(request, {
        "action": "concentration_book", "facts": [],
    }) with data.system.bundles as {"bundle": {"manifest": {"revision": "sha256:test"}}}
    result.allow
    result.allowed_ids == []
}

test_book_preflight_denies_nonfinancial_roles if {
    every role in ["ops", "engineer", "unknown"] {
        not authz.allow with input as object.union(request, {
            "action": "concentration_book_preflight", "resources": [],
            "principal": {"id": "TEST", "role": role},
        })
        not authz.allow with input as object.union(request, {
            "action": "concentration_book", "principal": {"id": "TEST", "role": role},
        })
    }
}

test_compliance_book_includes_uncovered_households if {
    result := authz.decision with input as object.union(request, {
        "action": "concentration_book", "resources": ["WC-HH-0003", "WC-HH-0001"],
        "principal": {"id": "CMP-01", "role": "compliance"}, "facts": [],
    }) with data.system.bundles as {"bundle": {"manifest": {"revision": "sha256:test"}}}
    result.allow
    result.allowed_ids == ["WC-HH-0001", "WC-HH-0003"]
}

test_search_filters_direct_and_team_candidates_but_exact_mode_denies_foreign if {
    candidate := object.union(request, {
        "action": "search_households", "resource_mode": "filter",
        "resources": ["WC-HH-0003", "WC-HH-0002", "WC-HH-0001"],
        "facts": array.concat(request.facts, team_facts),
    })
    result := authz.decision with input as candidate with data.system.bundles as {
        "bundle": {"manifest": {"revision": "sha256:test"}},
    }
    result.allow
    result.allowed_ids == ["WC-HH-0001", "WC-HH-0002"]
    result.projection == "full"
    not authz.allow with input as object.union(candidate, {"resource_mode": "exact"})
    not authz.allow with input as object.remove(candidate, ["resource_mode"])
}

test_search_preflight_and_filtered_roles if {
    every role in ["advisor", "compliance"] {
        authz.allow with input as object.union(request, {
            "action": "search_preflight", "resources": [],
            "principal": {"id": "TEST", "role": role},
        })
    }
    every role in ["ops", "engineer", "unknown"] {
        every action in ["search_preflight", "search_households"] {
            not authz.allow with input as object.union(request, {
                "action": action, "resource_mode": "filter", "resources": [],
                "principal": {"id": "TEST", "role": role},
            })
        }
    }
}

test_quality_and_feed_inventory_roles if {
    every role in ["ops", "compliance"] {
        authz.allow with input as object.union(request, {
            "action": "quality_preflight", "resources": [],
            "principal": {"id": "TEST", "role": role},
        })
        every action in ["view_quality", "feed_runs"] {
            authz.allow with input as object.union(request, {
                "action": action, "resources": ["WC-HH-0001", "WC-HH-0002"],
                "principal": {"id": "TEST", "role": role},
            })
        }
    }
    every role in ["advisor", "engineer", "unknown"] {
        every action in ["quality_preflight", "view_quality", "feed_runs"] {
            not authz.allow with input as object.union(request, {
                "action": action, "resources": [],
                "principal": {"id": "TEST", "role": role},
            })
        }
    }
}

test_ops_business_projection_and_full_metric_exception if {
    every action in ["view_quality", "feed_runs", "view_conflicts", "provenance", "trace_scope",
                     "conversation_context", "verify_claim"] {
        result := authz.decision with input as object.union(request, {
            "action": action, "principal": {"id": "OPS-01", "role": "ops"},
        }) with data.system.bundles as {"bundle": {"manifest": {"revision": "sha256:test"}}}
        result.allow
        result.projection == "no_pii"
        result.allowed_ids == request.resources
    }
    result := authz.decision with input as object.union(request, {
        "action": "metric_definition", "resources": [],
        "principal": {"id": "OPS-01", "role": "ops"},
    }) with data.system.bundles as {"bundle": {"manifest": {"revision": "sha256:test"}}}
    result.allow
    result.projection == "full"
    every action in ["snapshot", "portfolio_exposure", "concentrated_positions"] {
        not authz.allow with input as object.union(request, {
            "action": action, "principal": {"id": "OPS-01", "role": "ops"},
        })
    }
}

test_conflict_access_is_current_direct_or_team_or_global_inspector if {
    authz.allow with input as object.union(request, {"action": "view_conflicts"})
    authz.allow with input as object.union(request, {
        "action": "view_conflicts", "resources": ["WC-HH-0002"], "facts": team_facts,
    })
    not authz.allow with input as object.union(request, {
        "action": "view_conflicts", "resources": ["WC-HH-0003"],
    })
    every role in ["ops", "compliance"] {
        authz.allow with input as object.union(request, {
            "action": "view_conflicts", "resources": ["WC-HH-0003"],
            "principal": {"id": "TEST", "role": role},
        })
    }
    every role in ["engineer", "unknown"] {
        not authz.allow with input as object.union(request, {
            "action": "view_conflicts", "principal": {"id": "TEST", "role": role},
        })
    }
}

test_single_household_actions_never_allow_empty_or_multiple_scopes if {
    every action in ["snapshot", "portfolio_exposure", "view_conflicts"] {
        every resources in [[], ["WC-HH-0001", "WC-HH-0002"]] {
            every role in ["advisor", "ops", "compliance", "engineer"] {
                not authz.allow with input as object.union(request, {
                    "action": action, "resources": resources,
                    "principal": {"id": "ADV-01", "role": role},
                    "facts": array.concat(request.facts, team_facts),
                })
            }
        }
    }
}

test_decision_and_evaluation_inspectors_are_compliance_only if {
    every action in ["view_decision_log", "view_eval_reports"] {
        authz.allow with input as object.union(request, {
            "action": action, "resources": [],
            "principal": {"id": "COMP-01", "role": "compliance"},
        })
        every role in ["advisor", "ops", "engineer", "unknown"] {
            not authz.allow with input as object.union(request, {
                "action": action, "resources": [],
                "principal": {"id": "TEST", "role": role},
            })
        }
    }
}

test_trace_conversation_and_verification_reauthorize_every_household if {
    every action in ["trace_scope", "conversation_context", "verify_claim"] {
        authz.allow with input as object.union(request, {"action": action})
        not authz.allow with input as object.union(request, {
            "action": action, "resources": ["WC-HH-0001", "WC-HH-0003"],
        })
        authz.allow with input as object.union(request, {
            "action": action, "resources": [],
            "principal": {"id": "ENG-01", "role": "engineer"},
        })
        not authz.allow with input as object.union(request, {
            "action": action, "principal": {"id": "ENG-01", "role": "engineer"},
        })
        not authz.allow with input as object.union(request, {
            "action": action, "resources": [],
            "principal": {"id": "UNKNOWN", "role": "unknown"},
        })
    }
}

test_owned_error_metadata_policy_requires_known_role_and_empty_scope if {
    every role in ["advisor", "ops", "compliance", "engineer"] {
        authz.allow with input as object.union(request, {
            "action": "error_result", "resources": [],
            "principal": {"id": "TEST", "role": role},
        })
        not authz.allow with input as object.union(request, {
            "action": "error_result", "principal": {"id": "TEST", "role": role},
        })
    }
    not authz.allow with input as object.union(request, {
        "action": "error_result", "resources": [],
        "principal": {"id": "UNKNOWN", "role": "unknown"},
    })
}

# ADR 0009 section 6: household receipts are compliance only, exact, one household.
test_household_receipts_are_compliance_only_and_one_household if {
    authz.allow with input as object.union(request, {
        "action": "household_receipts_preflight", "resources": [],
        "principal": {"id": "COMP-01", "role": "compliance"},
    })
    authz.allow with input as object.union(request, {
        "action": "household_receipts", "resources": ["WC-HH-0009"],
        "principal": {"id": "COMP-01", "role": "compliance"},
    })
    every resources in [[], ["WC-HH-0001", "WC-HH-0002"]] {
        not authz.allow with input as object.union(request, {
            "action": "household_receipts", "resources": resources,
            "principal": {"id": "COMP-01", "role": "compliance"},
        })
    }
    not authz.allow with input as object.union(request, {
        "action": "household_receipts_preflight", "resources": ["WC-HH-0001"],
        "principal": {"id": "COMP-01", "role": "compliance"},
    })
    every role in ["advisor", "ops", "engineer", "unknown"] {
        every action in ["household_receipts_preflight", "household_receipts"] {
            not authz.allow with input as object.union(request, {
                "action": action, "resources": [],
                "principal": {"id": "ADV-01", "role": role},
            })
            not authz.allow with input as object.union(request, {
                "action": action, "principal": {"id": "ADV-01", "role": role},
            })
        }
    }
}

# ADR 0009 section 7: spans are refused to engineers and operations at the preflight.
test_trace_spans_preflight_admits_advisors_and_compliance_only if {
    every role in ["advisor", "compliance"] {
        authz.allow with input as object.union(request, {
            "action": "trace_spans_preflight", "resources": [],
            "principal": {"id": "ADV-01", "role": role},
        })
        not authz.allow with input as object.union(request, {
            "action": "trace_spans_preflight",
            "principal": {"id": "ADV-01", "role": role},
        })
    }
    every role in ["ops", "engineer", "unknown"] {
        every action in ["trace_spans_preflight", "trace_spans"] {
            not authz.allow with input as object.union(request, {
                "action": action, "resources": [],
                "principal": {"id": "OPS-01", "role": role},
            })
            not authz.allow with input as object.union(request, {
                "action": action, "principal": {"id": "OPS-01", "role": role},
            })
        }
    }
}

# An advisor must cover every household of the trace, by a current direct or team grant.
test_trace_spans_reauthorize_every_household_for_advisors if {
    authz.allow with input as object.union(request, {"action": "trace_spans"})
    authz.allow with input as object.union(request, {
        "action": "trace_spans", "resources": [],
    })
    not authz.allow with input as object.union(request, {
        "action": "trace_spans", "resources": ["WC-HH-0001", "WC-HH-0003"],
    })
    not authz.allow with input as object.union(request, {
        "action": "trace_spans", "request_business_date": "2026-10-10",
    })
    authz.allow with input as object.union(request, {
        "action": "trace_spans", "resources": ["WC-HH-0001", "WC-HH-0003"],
        "principal": {"id": "COMP-01", "role": "compliance"},
    })
    result := authz.decision with input as object.union(request, {"action": "trace_spans"})
        with data.system.bundles as {"bundle": {"manifest": {"revision": "sha256:test"}}}
    result.projection == "full"
    result.allowed_ids == ["WC-HH-0001"]
}
