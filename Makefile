.PHONY: up down test check schemas browser-check
up:
	docker compose up --build --wait
down:
	docker compose down
test: up
	docker compose --profile test build test
	docker compose run --rm --no-deps test pytest
	docker compose run --rm --no-deps --entrypoint /usr/local/bin/opa test test policies
check:
	uv run ruff check src tests reference_oracle scripts/build_bundle.py scripts/check_stdio.py scripts/check_workbench.py
	uv run pyright
	uv run pytest
schemas:
	uv run wealthcontext export-schemas

browser-check:
	uv run --group browser playwright install chromium
	uv run --group browser python scripts/check_workbench.py
