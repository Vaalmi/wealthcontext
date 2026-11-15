"""Page helpers for the compliance screens C1 to C3: structure only, never arithmetic.

Each helper arranges what one governed result returned. Nothing here adds, subtracts or
compares a figure: the span tree groups spans by their returned parent, and the receipts
lists are compared by run ID, as ADR 0009 section 6 says.
"""

from collections import defaultdict
from urllib.parse import urlencode


def trace_link(trace_id: str) -> str:
    """C3 for one trace. The page re-authorizes it: owner, coverage and recorded scope."""
    return "/workbench/trace?" + urlencode({"trace_id": trace_id})


def span_children(items: list[dict]) -> tuple[list[dict], dict[str, list[dict]]]:
    """The returned spans as roots and children, in the platform's own order.

    A span whose parent the platform omitted comes back with no parent and
    `parent_omitted` set, so it is a root here too."""
    children: dict[str, list[dict]] = defaultdict(list)
    roots = []
    for span in items:
        if span["parent_span_id"] is None:
            roots.append(span)
        else:
            children[span["parent_span_id"]].append(span)
    known = {span["span_id"] for span in items}
    # The platform returns every parent it names, so nothing is left unplaced; should a
    # parent ever be missing, its children are still shown rather than dropped.
    roots.extend(span for parent, group in children.items() if parent not in known
                 for span in group)
    return roots, dict(children)


def run_ids_only_in(receipts: dict | None, other: dict | None) -> set[str] | None:
    """Run IDs in one receipts list and not in the other, or None when either list is
    unavailable or capped, since a capped list cannot show what it left out."""
    if not receipts or not other:
        return None
    first, second = receipts.get("data"), other.get("data")
    if not isinstance(first, dict) or not isinstance(second, dict):
        return None
    if first["truncated"] or second["truncated"]:
        return None
    return ({item["run_id"] for item in first["items"]}
            - {item["run_id"] for item in second["items"]})
