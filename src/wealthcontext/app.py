"""REST and Streamable HTTP share one governed service and one captured request clock."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import JsonValue

from wealthcontext import __version__
from wealthcontext.bootstrap import bootstrap_history
from wealthcontext.chat_api import ProviderFactory, answer_chat, display_history
from wealthcontext.chat_contracts import (
    ChatArguments,
    ChatHistory,
    ChatRunResponse,
    HistoryArguments,
)
from wealthcontext.clock import Clock, SystemClock
from wealthcontext.context_service import (
    ConcentrationArguments,
    ContextService,
    ExposureArguments,
    MetricArguments,
    ProvenanceArguments,
    SnapshotArguments,
)
from wealthcontext.contracts import Contract, Envelope, ErrorCode
from wealthcontext.conversation import ConversationManager
from wealthcontext.database import migrate
from wealthcontext.exposure import ConcentrationEnvelope, ExposureEnvelope
from wealthcontext.harness import one_claim_demo
from wealthcontext.http_auth import Authenticate
from wealthcontext.identity import Identity
from wealthcontext.mcp_server import build_mcp
from wealthcontext.policy import PolicyClient, PolicyUnavailable
from wealthcontext.providers import create_provider as create_chat_provider
from wealthcontext.settings import Settings
from wealthcontext.snapshot import SnapshotEnvelope
from wealthcontext.storage import migrate_business
from wealthcontext.surface_contracts import (
    ConflictArguments,
    ConflictEnvelope,
    DecisionLogEnvelope,
    DecisionLogPageArguments,
    DecisionLogPageEnvelope,
    EmptyArguments,
    EvalReportsEnvelope,
    FeedHealthArguments,
    FeedHealthEnvelope,
    FeedRunImpactArguments,
    FeedRunImpactEnvelope,
    FeedRunsArguments,
    FeedRunsEnvelope,
    HouseholdReceiptsArguments,
    HouseholdReceiptsEnvelope,
    ListHouseholdsEnvelope,
    QualityArguments,
    QualityEnvelope,
    SearchArguments,
    SearchEnvelope,
    TraceArguments,
    TraceEnvelope,
    TraceSpansEnvelope,
)
from wealthcontext.telemetry import create_provider
from wealthcontext.workbench import install_workbench

ERROR_HTTP_STATUS: dict[ErrorCode, int] = {
    "UNAUTHENTICATED": 401,
    "NOT_AUTHORIZED": 403,
    "NOT_FOUND": 404,
    "INVALID_ARGUMENT": 422,
    "AS_OF_OUT_OF_RANGE": 422,
    "UNSUPPORTED_CURRENCY": 422,
    "RESULT_TOO_LARGE": 413,
    "POLICY_UNAVAILABLE": 503,
    "INTERNAL": 503,
}
REST_TOOLS = {
    "/api/get_household_snapshot": "get_household_snapshot",
    "/api/get_portfolio_exposure": "get_portfolio_exposure",
    "/api/get_concentrated_positions": "get_concentrated_positions",
    "/api/get_metric_definition": "get_metric_definition",
    "/api/get_data_provenance": "get_data_provenance",
    "/api/search_households": "search_households",
    "/api/list_households": "list_households",
    "/api/get_data_quality_report": "get_data_quality_report",
    "/api/feed_runs": "feed_runs",
    "/api/conflicts": "conflicts",
    "/api/decision_log": "decision_log",
    "/api/eval_reports": "eval_reports",
    "/api/trace": "trace",
    "/api/decision_log_page": "decision_log_page",
    "/api/household_receipts": "household_receipts",
    "/api/trace_spans": "trace_spans",
    "/api/feed_health": "feed_health",
    "/api/feed_run_impact": "feed_run_impact",
}


def _response(envelope: dict) -> JSONResponse:
    error = envelope.get("error")
    status = ERROR_HTTP_STATUS.get(error["code"], 500) if error else 200
    return JSONResponse(envelope, status_code=status)


def create_app(
    settings: Settings | None = None, clock: Clock | None = None,
    *, provider_factory: ProviderFactory = create_chat_provider,
) -> FastAPI:
    settings = settings or Settings.from_env()
    clock = clock or SystemClock()
    guidelines = (Path(__file__).resolve().parents[2] / "prompts/host_guidelines.md").read_text()
    mcp = build_mcp(guidelines)
    mcp_app = mcp.streamable_http_app(
        streamable_http_path="/mcp", json_response=True, stateless_http=True,
        transport_security=TransportSecuritySettings(
            allowed_hosts=["localhost", "localhost:*", "127.0.0.1", "127.0.0.1:*", "app:8000"],
            allowed_origins=["http://localhost:*", "http://127.0.0.1:*"],
        ),
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        policy = PolicyClient(settings.opa_url, settings.expected_revision)
        await policy.verify_revision()
        database = settings.data_dir / "wealthcontext.sqlite"
        migrate(database)
        migrate_business(database)
        bootstrap_history(database, settings.data_dir / "feeds", clock)
        spans = settings.data_dir / "traces.jsonl"
        provider = create_provider(spans)
        app.state.policy = policy
        app.state.settings = settings
        app.state.identity = Identity.load_or_create(settings.data_dir)
        app.state.service = ContextService(database, policy, provider.get_tracer("wealthcontext"),
                                           span_export=spans)
        try:
            async with mcp.session_manager.run():
                yield
        finally:
            provider.shutdown()

    app = FastAPI(title="WealthContext", version=__version__, lifespan=lifespan)
    app.add_middleware(Authenticate, application=app, clock=clock)

    @app.get("/health")
    async def health() -> JSONResponse:
        try:
            revision = await app.state.policy.verify_revision()
        except PolicyUnavailable:
            return JSONResponse({"status": "unavailable", "component": "policy"}, status_code=503)
        return JSONResponse(
            {"status": "ready", "policy_revision": revision, "version": __version__}
        )

    async def execute(request: Request, tool: str, arguments: Contract | None) -> JSONResponse:
        return _response(await app.state.service.execute(
            request.state.principal, request.state.request_now, tool,
            arguments.model_dump(mode="json") if arguments is not None else None,
        ))

    @app.exception_handler(RequestValidationError)
    async def invalid_arguments(request: Request, _error: RequestValidationError) -> JSONResponse:
        # FastAPI still publishes/validates the shared typed body. Rejections,
        # including malformed JSON, go through the same durable service evidence
        # path instead of returning FastAPI's ungoverned validation payload.
        return await execute(request, REST_TOOLS.get(request.url.path, "invalid_request"), None)

    errors: dict[int | str, dict[str, Any]] = {
        status: {"model": Envelope[dict[str, JsonValue]]}
        for status in set(ERROR_HTTP_STATUS.values())
    }

    @app.post("/api/get_household_snapshot", response_model=SnapshotEnvelope, responses=errors)
    async def household_snapshot(arguments: SnapshotArguments, request: Request) -> JSONResponse:
        return await execute(request, "get_household_snapshot", arguments)

    @app.post("/api/get_portfolio_exposure", response_model=ExposureEnvelope, responses=errors)
    async def portfolio_exposure(arguments: ExposureArguments, request: Request) -> JSONResponse:
        return await execute(request, "get_portfolio_exposure", arguments)

    @app.post("/api/get_concentrated_positions", response_model=ConcentrationEnvelope,
              responses=errors)
    async def concentrated_positions(
        arguments: ConcentrationArguments, request: Request,
    ) -> JSONResponse:
        return await execute(request, "get_concentrated_positions", arguments)

    @app.post("/api/get_metric_definition", response_model=Envelope[dict[str, JsonValue]],
              responses=errors)
    async def metric_definition(arguments: MetricArguments, request: Request) -> JSONResponse:
        return await execute(request, "get_metric_definition", arguments)

    @app.post("/api/get_data_provenance", response_model=Envelope[dict[str, JsonValue]],
              responses=errors)
    async def data_provenance(arguments: ProvenanceArguments, request: Request) -> JSONResponse:
        return await execute(request, "get_data_provenance", arguments)

    @app.post("/api/search_households", response_model=SearchEnvelope, responses=errors)
    async def search_households(arguments: SearchArguments, request: Request) -> JSONResponse:
        return await execute(request, "search_households", arguments)

    # ADR 0009 section 1: REST for the workbench only, never an MCP tool.
    @app.post("/api/list_households", response_model=ListHouseholdsEnvelope, responses=errors)
    async def list_households(arguments: EmptyArguments, request: Request) -> JSONResponse:
        return await execute(request, "list_households", arguments)

    @app.post("/api/get_data_quality_report", response_model=QualityEnvelope, responses=errors)
    async def quality_report(arguments: QualityArguments, request: Request) -> JSONResponse:
        return await execute(request, "get_data_quality_report", arguments)

    @app.post("/api/feed_runs", response_model=FeedRunsEnvelope, responses=errors)
    async def feed_runs(arguments: FeedRunsArguments, request: Request) -> JSONResponse:
        return await execute(request, "feed_runs", arguments)

    @app.post("/api/conflicts", response_model=ConflictEnvelope, responses=errors)
    async def conflicts(arguments: ConflictArguments, request: Request) -> JSONResponse:
        return await execute(request, "conflicts", arguments)

    @app.post("/api/decision_log", response_model=DecisionLogEnvelope, responses=errors)
    async def decision_log(arguments: EmptyArguments, request: Request) -> JSONResponse:
        return await execute(request, "decision_log", arguments)

    @app.post("/api/eval_reports", response_model=EvalReportsEnvelope, responses=errors)
    async def eval_reports(arguments: EmptyArguments, request: Request) -> JSONResponse:
        return await execute(request, "eval_reports", arguments)

    @app.post("/api/trace", response_model=TraceEnvelope, responses=errors)
    async def trace(arguments: TraceArguments, request: Request) -> JSONResponse:
        return await execute(request, "trace", arguments)

    # ADR 0009 sections 5 to 7: REST for the workbench only, never MCP tools.
    @app.post("/api/decision_log_page", response_model=DecisionLogPageEnvelope, responses=errors)
    async def decision_log_page(
        arguments: DecisionLogPageArguments, request: Request,
    ) -> JSONResponse:
        return await execute(request, "decision_log_page", arguments)

    @app.post("/api/household_receipts", response_model=HouseholdReceiptsEnvelope,
              responses=errors)
    async def household_receipts(
        arguments: HouseholdReceiptsArguments, request: Request,
    ) -> JSONResponse:
        return await execute(request, "household_receipts", arguments)

    @app.post("/api/trace_spans", response_model=TraceSpansEnvelope, responses=errors)
    async def trace_spans(arguments: TraceArguments, request: Request) -> JSONResponse:
        return await execute(request, "trace_spans", arguments)

    # ADR 0009 sections 3 and 4: REST for the workbench only, on the feed_runs action.
    @app.post("/api/feed_health", response_model=FeedHealthEnvelope, responses=errors)
    async def feed_health(arguments: FeedHealthArguments, request: Request) -> JSONResponse:
        return await execute(request, "feed_health", arguments)

    @app.post("/api/feed_run_impact", response_model=FeedRunImpactEnvelope, responses=errors)
    async def feed_run_impact(
        arguments: FeedRunImpactArguments, request: Request,
    ) -> JSONResponse:
        return await execute(request, "feed_run_impact", arguments)

    @app.post("/api/harness/answer", response_model=ChatRunResponse, responses=errors)
    async def harness_answer(arguments: ChatArguments, request: Request) -> JSONResponse:
        return _response(await answer_chat(
            app.state.service, request.state.principal, clock, arguments, provider_factory,
        ))

    @app.post("/api/harness/history", response_model=Envelope[ChatHistory], responses=errors)
    async def harness_history(arguments: HistoryArguments, request: Request) -> JSONResponse:
        return _response(await display_history(
            ConversationManager(app.state.service), request.state.principal, clock,
            arguments.conversation_id,
        ))

    @app.post("/api/harness/one-claim", response_model=Envelope[dict[str, JsonValue]],
              responses=errors)
    async def demo(request: Request) -> JSONResponse:
        return _response(await one_claim_demo(
            app.state.service, request.state.principal, request.state.request_now,
        ))

    # Child routes already contain /mcp. Mounting at root avoids redirecting the bearer request.
    install_workbench(app, clock, provider_factory)
    app.mount("/", mcp_app)

    return app
