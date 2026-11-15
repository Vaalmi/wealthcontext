FROM openpolicyagent/opa@sha256:3dba15e6a1109872e7142288dbc9edf6f0a75348feb5249d6e093cbf09e56a88 AS opa_binary
FROM ghcr.io/astral-sh/uv@sha256:4a1f3666816dafadd20ceb71eac9a7d56d7ef30279f7d64048519ab901ada336 AS uv_binary
FROM python@sha256:aa943ce411423c7582288f18707ff66eddcbaeb510502d747a72aeb34f30d159 AS build
COPY --from=uv_binary /uv /uvx /usr/local/bin/
COPY --from=opa_binary /opa /usr/local/bin/opa
WORKDIR /app
ENV UV_LINK_MODE=copy PYTHONUNBUFFERED=1 PATH="/app/.venv/bin:$PATH"
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-install-project
COPY src ./src
COPY reference_oracle ./reference_oracle
COPY scripts/build_bundle.py ./scripts/build_bundle.py
COPY policies ./policies
COPY schemas ./schemas
COPY fixtures ./fixtures
COPY acceptance ./acceptance
COPY prompts ./prompts
COPY tests ./tests
RUN uv sync --frozen && python scripts/build_bundle.py

FROM opa_binary AS policy_runtime
COPY --from=build /app/build/policy /bundle

FROM build AS app_runtime
ENV WEALTHCONTEXT_DATA_DIR=/data WEALTHCONTEXT_BUNDLE_DIR=/app/build/policy
RUN mkdir -p /data && chown -R 10001:10001 /data /app
USER 10001:10001
EXPOSE 8000
CMD ["wealthcontext", "serve"]
