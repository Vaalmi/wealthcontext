"""Capture request time and validate bearer identity once, before REST/MCP handlers."""

from fastapi import FastAPI
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.types import ASGIApp, Receive, Scope, Send

from wealthcontext.clock import Clock
from wealthcontext.identity import Unauthenticated
from wealthcontext.workbench_sessions import AUTH_WORKBENCH_PATHS, PUBLIC_WORKBENCH_PATHS


class Authenticate:
    def __init__(self, app: ASGIApp, *, application: FastAPI, clock: Clock):
        self.app, self.application, self.clock = app, application, clock

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "lifespan":
            await self.app(scope, receive, send)
            return
        if scope["type"] == "websocket":
            await send({"type": "websocket.close", "code": 1008})
            return
        if scope["path"] in {"/health", "/"} | PUBLIC_WORKBENCH_PATHS:
            await self.app(scope, receive, send)
            return
        now = self.clock.now()
        state = self.application.state
        try:
            if scope["path"] in AUTH_WORKBENCH_PATHS:
                session, principal = state.workbench_sessions.authenticate(
                    Request(scope), state.identity, now,
                )
                scope.setdefault("state", {})["demo_session"] = session
            else:
                principal = state.identity.authenticate(
                    Request(scope).headers.get("authorization"), now,
                )
        except Unauthenticated:
            # Audited durably for every route, before any response.
            envelope = state.service.authentication_error(now)
            page = getattr(state, "workbench_unauthenticated", None)
            if scope["path"] in AUTH_WORKBENCH_PATHS and page is not None:
                # A workbench page gets the restyled error page, not the API's JSON.
                response: Response = page()
            else:
                response = JSONResponse(envelope, status_code=401,
                                        headers={"WWW-Authenticate": "Bearer",
                                                 "Cache-Control": "no-store"})
            await response(scope, receive, send)
            return
        scope.setdefault("state", {}).update(
            principal=principal, request_now=now, service=state.service,
        )
        await self.app(scope, receive, send)
