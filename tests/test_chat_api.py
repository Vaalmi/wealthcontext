"""Real HTTP/auth/conversation/verifier flow with bounded, network-free provider turns."""

import ast
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx2
import pytest
from pydantic import JsonValue

from wealthcontext import providers
from wealthcontext.app import create_app
from wealthcontext.chat_api import _FAILURES
from wealthcontext.chat_contracts import ChatHistory, ChatRunResponse
from wealthcontext.clock import utc_timestamp
from wealthcontext.conversation import ConversationManager
from wealthcontext.database import write_transaction
from wealthcontext.harness import current_results
from wealthcontext.identity import Principal
from wealthcontext.policy import PolicyClient, PolicyDecision, PolicyUnavailable
from wealthcontext.provider_contracts import ProviderFailure, ProviderTurn, ToolCall
from wealthcontext.providers import create_provider
from wealthcontext.settings import Settings
from wealthcontext.storage import digest, read_snapshot, seed_vertical_slice
from wealthcontext.verifier import TEMPLATE

NOW = datetime(2035, 1, 1, 15, tzinfo=UTC)
REVISION = "sha256:" + "a" * 64
ARGS: dict[str, JsonValue] = {"household_id": "WC-HH-0001", "as_of": "2026-09-30"}
PROVIDER_SECRET = "UNVERIFIED_PROVIDER_SENTINEL_989898"
CANDIDATE_SECRET = "INVALID_CANDIDATE_SENTINEL_878787"


class TickClock:
    def __init__(self):
        self.instant = NOW
        self.calls = 0

    def now(self):
        self.calls += 1
        self.instant += timedelta(microseconds=1)
        return self.instant


class ScriptedProvider:
    provider = "openai"
    model = "network-free-http-fixture"
    settings: dict[str, JsonValue] = {}

    def __init__(self, *, invalid_attempts=0, on_close=None):
        self.requests = []
        self.invalid_attempts = invalid_attempts
        self.on_close = on_close
        self.closed = False

    async def complete(self, request):
        self.requests.append(request)
        if len(self.requests) == 1:
            call = ToolCall(id="fetch", name="get_household_snapshot", arguments=ARGS)
        else:
            results = current_results({"data": {"messages": request.messages}})
            denied = next((result for result in results.values() if result["status"] == "ERROR"),
                          None)
            if denied is not None:
                return ProviderTurn(model=self.model, calls=[ToolCall(
                    id="refusal", name="submit_answer", arguments={
                        "answer_markdown": "Requested data is unavailable.", "claims": [],
                        "predicates": [], "caveats": [], "refusal": {
                            "code": denied["error"]["code"], "result_id": denied["result_id"],
                        },
                    },
                )])
            snapshot = next(result for result in results.values()
                            if (result.get("data") or {}).get("household_id") == "WC-HH-0001")
            invalid = len(self.requests) - 1 <= self.invalid_attempts
            call = ToolCall(id=f"answer{len(self.requests)}", name="submit_answer", arguments={
                "answer_markdown": CANDIDATE_SECRET if invalid else TEMPLATE,
                "claims": [{"id": "c1", "result_id": snapshot["result_id"],
                            "entity": {"type": "household", "id": "WC-HH-0001"},
                            "field": "assets_usd", "household_id": "WC-HH-0001",
                            "valuation_date": "2026-09-30", "unit": "USD",
                            "format": "value_only"}],
                "predicates": [], "caveats": [], "refusal": None,
            })
        return ProviderTurn(calls=[call], model=self.model, text=PROVIDER_SECRET,
                            request_id="SECRET_SDK_REQUEST", usage={"hidden": "SDK_METADATA"})

    async def aclose(self):
        self.closed = True
        if self.on_close:
            self.on_close()


@pytest.fixture
async def chat(tmp_path, monkeypatch):
    (tmp_path / "revision.txt").write_text(REVISION)
    state: dict[str, Any] = {"outage": False, "factory_calls": []}

    async def revision(_self):
        return REVISION

    async def decide(_self, principal, action, resources, facts, revision, request_business_date):
        if state["outage"]:
            raise PolicyUnavailable("sensitive transport details")
        covered = {fact["object"] for fact in facts
                   if fact["kind"] == "advisor_covers" and fact["subject"] == principal.id}
        allow = not resources or principal.role == "compliance" or (
            principal.role == "advisor" and set(resources) <= covered
        )
        projection = ("no_pii" if principal.role == "ops" and action != "metric_definition"
                      else "full")
        return PolicyDecision(allow=allow, allowed_ids=resources if allow else [],
                              projection=projection, reason="GRANTED" if allow else "DENIED",
                              policy_revision=REVISION, entitlement_revision=revision)

    monkeypatch.setattr(PolicyClient, "verify_revision", revision)
    monkeypatch.setattr(PolicyClient, "decide", decide)
    def bootstrap(database, _directory, clock):
        seed_vertical_slice(database, clock.now())

    monkeypatch.setattr("wealthcontext.app.bootstrap_history", bootstrap)
    state["adapter"] = ScriptedProvider()

    def factory(provider):
        state["factory_calls"].append(provider)
        if "factory" in state:
            return state["factory"](provider)
        return state["adapter"]

    clock = TickClock()
    app = create_app(Settings(tmp_path, tmp_path, "http://unused.invalid"), clock,
                     provider_factory=factory)
    async with app.router.lifespan_context(app):
        async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app),
                                      base_url="http://localhost:8000") as client:
            yield app, client, clock, state, tmp_path / "wealthcontext.sqlite"


def bearer(app, persona="ADV-01", *, expired=False):
    return {"Authorization": "Bearer " + app.state.identity.mint(persona, NOW, expired=expired)}


async def ask(app, client, **extra):
    return await client.post("/api/harness/answer", headers=bearer(app),
                             json={"question": "Show household assets.", **extra})


def revoke(database):
    with write_transaction(database) as connection:
        revision = connection.execute(
            "SELECT MAX(revision)+1 FROM entitlement_revisions",
        ).fetchone()[0]
        connection.execute("INSERT INTO entitlement_revisions VALUES (?,?)",
                           (revision, utc_timestamp(NOW)))


def safe_display(payload):
    serialized = json.dumps(payload)
    for forbidden in (PROVIDER_SECRET, CANDIDATE_SECRET, "SECRET_SDK_REQUEST", "SDK_METADATA",
                      '"tool_calls"', '"terminal_attempts"', '"turns"', '"authorized_result_ids"'):
        assert forbidden not in serialized


@pytest.mark.anyio
async def test_answer_returns_only_fresh_verified_display_and_public_metadata(chat):
    app, client, clock, state, database = chat
    response = await ask(app, client)
    assert response.status_code == 200, response.text
    payload = response.json()
    result = ChatRunResponse.model_validate(payload)
    assert result.mode == "live_chat" and result.provider == "openai"
    assert result.run_id and result.conversation_id and result.model == state["adapter"].model
    assert result.first_pass_valid and result.final_valid
    assert not result.fallback and not result.retries and result.failure is None
    assert result.final_result is not None
    markdown = result.final_result.data["markdown"]
    assert isinstance(markdown, str) and "USD 3000.00" in markdown
    assert state["factory_calls"] == ["openai"] and state["adapter"].closed
    assert clock.calls > len(state["adapter"].requests) * 2
    safe_display(payload)
    history = await client.post("/api/harness/history", headers=bearer(app),
                                json={"conversation_id": result.conversation_id})
    assert history.status_code == 200
    data = ChatHistory.model_validate(history.json()["data"])
    assert [message.role for message in data.messages] == ["user", "assistant"]
    assert data.messages[0].content == "Show household assets."
    assert data.messages[-1].data == result.final_result.data
    safe_display(history.json())
    with read_snapshot(database) as connection:
        recorded = connection.execute(
            "SELECT payload,payload_hash FROM results WHERE id=?",
            (history.json()["result_id"],),
        ).fetchone()
        assert json.loads(recorded[0]) == history.json()
        assert digest(recorded[0]) == recorded[1]
        assert connection.execute(
            "SELECT COUNT(DISTINCT recorded_at) FROM results",
        ).fetchone()[0] > 3
        assert connection.execute("SELECT COUNT(*) FROM decisions").fetchone()[0] > 3


@pytest.mark.anyio
@pytest.mark.parametrize("invalid_attempts,expected_fallback", [(1, False), (2, True)])
async def test_retry_and_fallback_do_not_disclose_terminal_candidates_or_diagnostics(
    chat, invalid_attempts, expected_fallback,
):
    app, client, _clock, state, _database = chat
    state["adapter"] = ScriptedProvider(invalid_attempts=invalid_attempts)
    response = await ask(app, client)
    assert response.status_code == 200, response.text
    payload = response.json()
    assert not payload["first_pass_valid"] and payload["final_valid"]
    assert payload["retries"] == 1 and payload["fallback"] is expected_fallback
    safe_display(payload)
    history = await client.post("/api/harness/history", headers=bearer(app),
                                json={"conversation_id": payload["conversation_id"]})
    safe_display(history.json())
    assert len(history.json()["data"]["messages"]) == 2


@pytest.mark.anyio
async def test_revoke_after_harness_finish_removes_answer_at_http_boundary_and_history(chat):
    app, client, _clock, state, database = chat
    state["adapter"] = ScriptedProvider(on_close=lambda: revoke(database))
    response = await ask(app, client)
    payload = response.json()
    assert response.status_code == 200
    assert not payload["final_valid"] and payload["final_result"] is None
    assert payload["failure"] == "ANSWER_REVOKED_OR_UNAVAILABLE"
    assert "3000.00" not in response.text
    history = await client.post("/api/harness/history", headers=bearer(app),
                                json={"conversation_id": payload["conversation_id"]})
    assert history.status_code == 200
    last = history.json()["data"]["messages"][-1]
    assert last["redacted"] and last["data"] is None
    assert "3000.00" not in history.text
    safe_display(history.json())


@pytest.mark.anyio
async def test_missing_expired_and_forged_identity_cannot_construct_provider(chat):
    app, client, _clock, state, database = chat
    for headers in ({}, {"Authorization": "Bearer invented"}, bearer(app, expired=True)):
        for path, body in (("answer", {"question": "anything"}),
                           ("history", {"conversation_id": "conv_unknown"})):
            response = await client.post(f"/api/harness/{path}", headers=headers, json=body)
            assert response.status_code == 401
            assert response.json()["error"]["code"] == "UNAUTHENTICATED"
    assert state["factory_calls"] == []
    with read_snapshot(database) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM decisions WHERE reason='UNAUTHENTICATED'",
        ).fetchone()[0] == 6


@pytest.mark.anyio
async def test_foreign_and_unknown_conversations_are_indistinguishable_before_provider(chat):
    app, client, clock, state, database = chat
    foreign = ConversationManager(app.state.service).start(
        Principal("ADV-02", "advisor"), clock.now(),
    )
    for conversation in (foreign, "conv_unknown"):
        answer = await ask(app, client, conversation_id=conversation)
        history = await client.post("/api/harness/history", headers=bearer(app),
                                    json={"conversation_id": conversation})
        for response in (answer, history):
            assert response.status_code == 403
            assert response.json()["error"]["code"] == "NOT_AUTHORIZED"
            assert response.json()["data"] is None
    assert state["factory_calls"] == []
    with read_snapshot(database) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM decisions WHERE reason='CONVERSATION_OWNER_MISMATCH'",
        ).fetchone()[0] == 4


@pytest.mark.anyio
async def test_policy_outage_blocks_provider_and_whole_history_view(chat):
    app, client, _clock, state, _database = chat
    successful = (await ask(app, client)).json()
    state["outage"] = True
    answer = await ask(app, client, conversation_id=successful["conversation_id"])
    history = await client.post("/api/harness/history", headers=bearer(app),
                                json={"conversation_id": successful["conversation_id"]})
    for response in (answer, history):
        assert response.status_code == 503
        assert response.json()["error"]["code"] == "POLICY_UNAVAILABLE"
        assert response.json()["data"] is None and "3000.00" not in response.text
        assert "sensitive transport" not in response.text
    assert state["factory_calls"] == ["openai"]


def without_provider_credentials(monkeypatch) -> list[str]:
    """SOL-C-R2-TEST-001: clear every provider key, whichever provider is the default, and
    record any SDK client construction. The chat API converts exceptions into a 503, so the
    returned list, not the raised error, is what tests assert stays empty."""
    for name in ("ANTHROPIC_API_KEY", "ANTHROPIC_WORKSPACE_ID", "OPENAI_API_KEY",
                 "GEMINI_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    constructed: list[str] = []

    def guard(name):
        def unexpected_client(*_args, **_kwargs):
            constructed.append(name)
            raise AssertionError("an SDK client was constructed")
        return unexpected_client
    monkeypatch.setattr(providers.anthropic, "AsyncAnthropic", guard("anthropic"))
    monkeypatch.setattr(providers.openai, "AsyncOpenAI", guard("openai"))
    monkeypatch.setattr(providers.genai, "Client", guard("google"))
    return constructed


@pytest.mark.anyio
async def test_the_credential_guard_records_construction_the_api_would_swallow(chat, monkeypatch):
    app, client, _clock, state, _database = chat
    constructed = without_provider_credentials(monkeypatch)
    monkeypatch.setenv("OPENAI_API_KEY", "synthetic-not-a-key")
    state["factory"] = create_provider
    response = await ask(app, client)
    assert response.status_code == 503  # The API hides the guard's exception ...
    assert constructed == ["openai"]  # ... but the guard still records the construction.


@pytest.mark.anyio
async def test_missing_default_credentials_is_controlled_and_durably_audited(chat, monkeypatch):
    app, client, _clock, state, database = chat
    constructed = without_provider_credentials(monkeypatch)
    state["factory"] = create_provider
    response = await ask(app, client)
    assert response.status_code == 503 and constructed == []
    envelope = response.json()
    assert envelope["error"]["details"] == {"failure": "MISSING_PROVIDER_CREDENTIALS"}
    assert state["adapter"].requests == []
    with read_snapshot(database) as connection:
        assert json.loads(connection.execute("SELECT payload FROM results WHERE id=?",
                                             (envelope["result_id"],)).fetchone()[0]) == envelope
        assert connection.execute(
            "SELECT reason FROM decisions WHERE trace_id=?", (envelope["trace_id"],),
        ).fetchone()[0] == "MISSING_PROVIDER_CREDENTIALS"


@pytest.mark.anyio
async def test_unexpected_factory_exception_is_never_returned(chat):
    app, client, _clock, state, _database = chat

    def explode(_provider):
        raise RuntimeError("sk-secret and PRIVATE_CUSTOMER_NAME")

    state["factory"] = explode
    response = await ask(app, client)
    assert response.status_code == 503
    assert "sk-secret" not in response.text and "PRIVATE_CUSTOMER" not in response.text
    assert response.json()["error"]["details"] == {"failure": "HARNESS_FAILED"}


_SOURCE = Path(__file__).resolve().parents[1] / "src/wealthcontext"
# Every sanitized code a live chat run can carry: provider adapters, harness
# classifications and the closed envelope codes that reach run.failure.
EMITTED_FAILURES = frozenset({
    "MISSING_PROVIDER_CREDENTIALS", "PROVIDER_CALL_FAILED", "MALFORMED_PROVIDER_RESPONSE",
    "MALFORMED_TOOL_ARGUMENTS", "PROVIDER_OUTPUT_LIMIT", "PROVIDER_OUTPUT_INCOMPLETE",
    "PROVIDER_TIMEOUT", "NATIVE_TOOL_CALL_REQUIRED", "DUPLICATE_TOOL_CALL_ID",
    "MIXED_TERMINAL_TOOL_CALL", "TERMINAL_TIMEOUT", "ANSWER_REVOKED_OR_UNAVAILABLE",
    "VERIFICATION_FAILED", "UNEXPECTED_DATA_TOOL", "DATA_TOOL_BUDGET_EXCEEDED",
    "DATA_TOOL_TIMEOUT", "TOTAL_OR_TOOL_TIMEOUT", "HARNESS_FAILED",
    "FALLBACK_REVOKED_OR_UNAVAILABLE", "FALLBACK_VERIFICATION_FAILED",
    "INVALID_AUTHORIZED_CONTEXT", "NOT_AUTHORIZED", "POLICY_UNAVAILABLE", "INTERNAL",
})


def literal_failures(module: str) -> set[str]:
    """String codes passed to ProviderFailure or assigned to run.failure / turn.error."""
    codes = set()
    for node in ast.walk(ast.parse((_SOURCE / module).read_text())):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                and node.func.id == "ProviderFailure" and node.args
                and isinstance(node.args[0], ast.Constant)):
            codes.add(node.args[0].value)
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant) and any(
            isinstance(target, ast.Attribute) and target.attr in {"failure", "error"}
            for target in node.targets
        ):
            codes.add(node.value.value)
    return codes


def test_failure_allowlist_is_exactly_the_closed_emitted_set():
    """ARGON-P5-CHAT-ROUTES-R1-001: no emitted code collapses; nothing unemitted is listed."""
    assert literal_failures("providers.py") <= EMITTED_FAILURES
    assert literal_failures("harness.py") <= EMITTED_FAILURES
    assert _FAILURES == EMITTED_FAILURES


class FailingProvider(ScriptedProvider):
    def __init__(self, code):
        super().__init__()
        self.code = code

    async def complete(self, request):
        self.requests.append(request)
        raise ProviderFailure(self.code)


@pytest.mark.anyio
@pytest.mark.parametrize("code,expected", [
    *((code, code) for code in sorted(EMITTED_FAILURES)),
    ("SDK_SAID_sk-secret", "HARNESS_FAILED"), ("INVALID_PROVIDER_RESPONSE", "HARNESS_FAILED"),
])
async def test_sanitized_failure_codes_survive_and_unknown_codes_collapse(chat, code, expected):
    app, client, _clock, state, _database = chat
    state["adapter"] = FailingProvider(code)
    response = await ask(app, client)
    assert response.status_code == 200, response.text
    payload = ChatRunResponse.model_validate(response.json())
    assert payload.failure == expected
    assert payload.fallback and payload.final_valid and state["adapter"].closed
    assert "sk-secret" not in response.text


@pytest.mark.anyio
@pytest.mark.parametrize("extra", [
    {"question": " "}, {"provider": "unknown"}, {"mode": "live_scored"}, {"scored": True},
    {"principal": "COMP-01"}, {"conversation_id": "invalid id"},
    {"known_as_of": "2026-09-30T10:00:00"}, {"question": "x" * 12001},
    {"question": '"' * 12000, "as_of": "2026-09-30"},
])
async def test_invalid_or_privileged_body_fields_are_audited_before_provider(chat, extra):
    app, client, _clock, state, database = chat
    response = await ask(app, client, **extra)
    assert response.status_code == 422
    envelope = response.json()
    assert envelope["error"]["code"] == "INVALID_ARGUMENT"
    assert state["factory_calls"] == []
    with read_snapshot(database) as connection:
        assert connection.execute("SELECT 1 FROM results WHERE id=?",
                                  (envelope["result_id"],)).fetchone()


@pytest.mark.anyio
async def test_requested_cutoffs_are_model_context_never_authorization_time(chat):
    app, client, _clock, state, database = chat
    response = await ask(app, client, as_of="2026-09-30", known_as_of="2026-10-01T15:00:00Z")
    assert response.status_code == 200
    first_request = state["adapter"].requests[0]
    question = next(message["content"] for message in first_request.messages
                    if message["role"] == "user")
    assert json.loads(question) == {"question": "Show household assets.", "as_of": "2026-09-30",
                                    "known_as_of": "2026-10-01T15:00:00Z"}
    with read_snapshot(database) as connection:
        times = [datetime.fromisoformat(row[0])
                 for row in connection.execute("SELECT recorded_at FROM decisions")]
    assert all(instant >= NOW for instant in times)


def test_chat_schema_has_no_scored_mode_identity_or_provider_evidence():
    schema = ChatRunResponse.model_json_schema()
    assert set(schema["properties"]) == {
        "run_id", "conversation_id", "provider", "model", "mode", "first_pass_valid", "final_valid",
        "retries", "fallback", "failure", "final_result",
    }
    assert schema["properties"]["mode"]["const"] == "live_chat"


@pytest.mark.anyio
async def test_every_chat_result_path_has_complete_bound_scope_evidence(chat, monkeypatch):
    app, client, _clock, state, database = chat
    state["adapter"] = ScriptedProvider(invalid_attempts=2)
    successful = (await ask(app, client)).json()
    assert successful["fallback"] and successful["final_valid"]
    history = await client.post("/api/harness/history", headers=bearer(app),
                                json={"conversation_id": successful["conversation_id"]})
    assert history.status_code == 200
    constructed = without_provider_credentials(monkeypatch)
    state["factory"] = create_provider
    missing = await ask(app, client)
    assert missing.status_code == 503 and constructed == []
    assert missing.json()["error"]["details"] == {"failure": "MISSING_PROVIDER_CREDENTIALS"}
    unauthenticated = await client.post("/api/harness/answer",
                                        json={"question": "unauthenticated"})
    assert unauthenticated.status_code == 401
    with read_snapshot(database) as connection:
        results = connection.execute("SELECT id FROM results").fetchall()
        assert len(results) == connection.execute(
            "SELECT COUNT(*) FROM result_scope_integrity",
        ).fetchone()[0]
        for row in results:
            app.state.service.result_scopes(connection, row[0])
        actions = {row[0] for row in connection.execute("SELECT action FROM result_access")}
        assert {"snapshot", "verify_claim", "error_result", "conversation_context"} <= actions


@pytest.mark.anyio
async def test_real_opa_preserves_role_boundaries_through_chat_and_history(
    tmp_path, monkeypatch, opa_server,
):
    """Runs with the real OPA fixture when available; SDK transport stays synthetic."""
    (tmp_path / "revision.txt").write_text(opa_server.expected_revision)

    def bootstrap(database, _directory, clock):
        seed_vertical_slice(database, clock.now())

    monkeypatch.setattr("wealthcontext.app.bootstrap_history", bootstrap)
    adapters = []

    def factory(_provider):
        adapter = ScriptedProvider()
        adapters.append(adapter)
        return adapter

    app = create_app(Settings(tmp_path, tmp_path, opa_server.url), TickClock(),
                     provider_factory=factory)
    async with app.router.lifespan_context(app):
        async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app),
                                      base_url="http://localhost:8000") as client:
            for persona in ("ADV-01", "COMP-01", "OPS-01", "ENG-01"):
                response = await client.post("/api/harness/answer", headers=bearer(app, persona),
                                             json={"question": "Show household assets."})
                assert response.status_code == 200, response.text
                payload = response.json()
                assert payload["final_valid"], payload
                if persona in {"OPS-01", "ENG-01"}:
                    assert "3000.00" not in response.text
                    assert payload["final_result"]["data"]["refusal"]["code"] == "NOT_AUTHORIZED"
                else:
                    assert "USD 3000.00" in payload["final_result"]["data"]["markdown"]
                history = await client.post("/api/harness/history", headers=bearer(app, persona),
                                            json={"conversation_id": payload["conversation_id"]})
                assert history.status_code == 200, history.text
                safe_display(history.json())
                denied = await client.post("/api/harness/history", headers=bearer(app, "ADV-02"),
                                           json={"conversation_id": payload["conversation_id"]})
                assert denied.status_code == 403
    assert len(adapters) == 4
