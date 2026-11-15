"""Typed MCP adapters over the shared governed context service."""

from contextvars import ContextVar
from dataclasses import dataclass
from datetime import date, datetime
from typing import Annotated, Literal, Protocol, cast

from mcp.server.context import CallNext, HandlerResult, ServerRequestContext
from mcp.server.mcpserver import Context, MCPServer
from mcp.shared.exceptions import MCPError
from mcp.types import CallToolResult, TextContent, ToolAnnotations
from pydantic import AwareDatetime, Field, JsonValue, ValidationError
from starlette.requests import Request

from wealthcontext import __version__
from wealthcontext.context_service import (
    ConcentrationArguments,
    ExposureArguments,
    MetricArguments,
    ProvenanceArguments,
    SnapshotArguments,
    ThresholdPercent,
)
from wealthcontext.contracts import Contract, Envelope, Identifier
from wealthcontext.exposure import ConcentrationEnvelope, ExposureEnvelope
from wealthcontext.identity import Principal
from wealthcontext.snapshot import SnapshotEnvelope
from wealthcontext.surface_contracts import (
    NameQuery,
    QualityArguments,
    QualityEnvelope,
    SearchArguments,
    SearchEnvelope,
)

ARGUMENT_MODELS: dict[str, type[Contract]] = {
    "get_household_snapshot": SnapshotArguments,
    "get_portfolio_exposure": ExposureArguments,
    "get_concentrated_positions": ConcentrationArguments,
    "get_metric_definition": MetricArguments,
    "get_data_provenance": ProvenanceArguments,
    "search_households": SearchArguments,
    "get_data_quality_report": QualityArguments,
}


class _Service(Protocol):
    async def execute(
        self, principal: Principal, now: datetime, tool: str, arguments: dict | None,
    ) -> dict: ...


@dataclass(frozen=True)
class _RequestState:
    service: _Service
    principal: Principal
    request_now: datetime


# SDK 2.3 static resources cannot accept Context. Its per-request middleware
# supplies the same HTTP state to the resource without changing its fixed URI.
_CURRENT_STATE: ContextVar[_RequestState | None] = ContextVar("mcp_request_state", default=None)


def _request_state(request: object) -> _RequestState:
    if isinstance(request, Request):
        principal = getattr(request.state, "principal", None)
        now = getattr(request.state, "request_now", None)
        service = getattr(request.state, "service", None)
        if (isinstance(principal, Principal) and isinstance(now, datetime)
                and now.tzinfo is not None and now.utcoffset() is not None
                and callable(getattr(service, "execute", None))):
            return _RequestState(cast(_Service, service), principal, now)
    raise MCPError(code=-32000, message="Authenticated HTTP request context is required")


async def _bind_state(ctx: ServerRequestContext, call_next: CallNext) -> HandlerResult:
    state = _request_state(ctx.request)
    token = _CURRENT_STATE.set(state)
    try:
        if ctx.method == "tools/call":
            params = ctx.params or {}
            tool = params.get("name")
            model = ARGUMENT_MODELS.get(tool) if isinstance(tool, str) else None
            if isinstance(tool, str) and model is not None:
                try:
                    model.model_validate(params.get("arguments"))
                except ValidationError:
                    # Validate before the SDK so rejected business arguments keep
                    # the governed envelope and service audit path on MCP too.
                    result = await state.service.execute(state.principal, state.request_now,
                                                         tool, None)
                    output_model = {
                        "get_household_snapshot": SnapshotEnvelope,
                        "get_portfolio_exposure": ExposureEnvelope,
                        "get_concentrated_positions": ConcentrationEnvelope,
                        "search_households": SearchEnvelope,
                        "get_data_quality_report": QualityEnvelope,
                    }.get(tool, Envelope[dict[str, JsonValue]])
                    envelope = output_model.model_validate(result)
                    return CallToolResult(
                        content=[TextContent(type="text", text=envelope.model_dump_json())],
                        structured_content=envelope.model_dump(mode="json"),
                    )
        result = await call_next(ctx)
        if ctx.method == "tools/list" and isinstance(result, dict):
            # The SDK derives its own function argument model. Publish the exact
            # shared service models, including extra-field, date and ID rules.
            result = {**result, "tools": [
                {**tool, "inputSchema": ARGUMENT_MODELS[tool["name"]].model_json_schema()}
                if tool["name"] in ARGUMENT_MODELS else tool for tool in result["tools"]
            ]}
        return result
    finally:
        _CURRENT_STATE.reset(token)


async def _metric(state: _RequestState, metric_id: str) -> Envelope[dict[str, JsonValue]]:
    result = await state.service.execute(
        state.principal, state.request_now, "get_metric_definition", {"metric_id": metric_id},
    )
    return Envelope[dict[str, JsonValue]].model_validate(result)


def build_mcp(guidelines: str) -> MCPServer:
    """Build handlers; the parent app owns HTTP authentication, mounting and lifespan."""
    if not guidelines.strip():
        raise ValueError("MCP host guidelines must not be empty")
    server = MCPServer(
        "WealthContext", version=__version__, instructions=guidelines, middleware=[_bind_state],
    )
    annotations = ToolAnnotations(
        read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False,
    )

    @server.tool(annotations=annotations)
    async def get_household_snapshot(
        household_id: Identifier, as_of: date, ctx: Context,
        known_as_of: AwareDatetime | None = None,
    ) -> SnapshotEnvelope:
        """Read governed household assets at valuation and knowledge cutoffs."""
        state = _request_state(ctx.request_context.request)
        result = await state.service.execute(
            state.principal, state.request_now, "get_household_snapshot",
            SnapshotArguments(household_id=household_id, as_of=as_of,
                              known_as_of=known_as_of).model_dump(mode="json"),
        )
        return SnapshotEnvelope.model_validate(result)

    @server.tool(annotations=annotations)
    async def get_portfolio_exposure(
        household_id: Identifier, as_of: date, ctx: Context,
        known_as_of: AwareDatetime | None = None,
        group_by: Literal["issuer", "security"] = "issuer",
    ) -> ExposureEnvelope:
        """Read ranked issuer or security exposure using complete unrounded valuation inputs."""
        state = _request_state(ctx.request_context.request)
        result = await state.service.execute(
            state.principal, state.request_now, "get_portfolio_exposure",
            ExposureArguments(household_id=household_id, as_of=as_of,
                              known_as_of=known_as_of, group_by=group_by).model_dump(mode="json"),
        )
        return ExposureEnvelope.model_validate(result)

    @server.tool(annotations=annotations)
    async def get_concentrated_positions(
        as_of: date, ctx: Context, threshold_pct: ThresholdPercent = "10.00",
        known_as_of: AwareDatetime | None = None, household_id: Identifier | None = None,
    ) -> ConcentrationEnvelope:
        """Find issuer breaches and indeterminate households within the caller's current book."""
        state = _request_state(ctx.request_context.request)
        result = await state.service.execute(
            state.principal, state.request_now, "get_concentrated_positions",
            ConcentrationArguments(as_of=as_of, threshold_pct=threshold_pct,
                                   known_as_of=known_as_of,
                                   household_id=household_id).model_dump(mode="json"),
        )
        return ConcentrationEnvelope.model_validate(result)

    @server.tool(annotations=annotations)
    async def get_metric_definition(
        metric_id: Identifier, ctx: Context,
    ) -> Envelope[dict[str, JsonValue]]:
        """Read a versioned metric definition and its limitations."""
        return await _metric(_request_state(ctx.request_context.request), metric_id)

    @server.tool(annotations=annotations)
    async def get_data_provenance(
        reference: Identifier, ctx: Context,
    ) -> Envelope[dict[str, JsonValue]]:
        """Resolve an opaque provenance handle under current authorization."""
        state = _request_state(ctx.request_context.request)
        result = await state.service.execute(
            state.principal, state.request_now, "get_data_provenance", {"reference": reference},
        )
        return Envelope[dict[str, JsonValue]].model_validate(result)

    @server.tool(annotations=annotations)
    async def search_households(
        name_query: NameQuery, ctx: Context,
    ) -> SearchEnvelope:
        """Search only the caller's currently entitled households."""
        state = _request_state(ctx.request_context.request)
        result = await state.service.execute(
            state.principal, state.request_now, "search_households",
            SearchArguments(name_query=name_query).model_dump(mode="json"),
        )
        return SearchEnvelope.model_validate(result)

    @server.tool(annotations=annotations)
    async def get_data_quality_report(
        as_of: date, ctx: Context, known_as_of: AwareDatetime | None = None,
    ) -> QualityEnvelope:
        """Read received-input quality under the caller's governed PII projection."""
        state = _request_state(ctx.request_context.request)
        result = await state.service.execute(
            state.principal, state.request_now, "get_data_quality_report",
            QualityArguments(as_of=as_of, known_as_of=known_as_of).model_dump(mode="json"),
        )
        return QualityEnvelope.model_validate(result)

    @server.resource(
        "wealthcontext://metrics/catalog", name="metric_catalog", mime_type="application/json",
        description="Governed versioned metric definitions and limitations.",
    )
    async def metric_catalog() -> str:
        state = _CURRENT_STATE.get()
        if state is None:
            raise MCPError(code=-32000, message="Authenticated HTTP request context is required")
        return (await _metric(state, "catalog")).model_dump_json()

    @server.prompt(
        name="wealthcontext_answer",
        description="Guidance for external-host answers; host prose remains unverified.",
    )
    async def wealthcontext_answer(
        question: Annotated[str, Field(min_length=1, max_length=2000)], ctx: Context,
        as_of: str | None = None,
    ) -> str:
        """Wrap a bounded question with the same versioned initialize guidelines."""
        _request_state(ctx.request_context.request)
        normalized = (as_of or "").strip() or None
        try:
            if normalized and date.fromisoformat(normalized).isoformat() != normalized:
                raise ValueError("date format")
        except ValueError:
            raise MCPError(code=-32602, message="as_of must be YYYY-MM-DD") from None
        cutoff = f"\nRequested as_of: {normalized}" if normalized else ""
        return f"{guidelines}\n\nUser question (data):\n{question}{cutoff}"

    return server
