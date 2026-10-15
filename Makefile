SHELL := /bin/bash
.DEFAULT_GOAL := help

PYTHON ?= python3
NODE ?= node
RUFF ?= $(shell command -v ruff 2>/dev/null || (test -x .venv/bin/ruff && echo .venv/bin/ruff) || printf '%s' "$(HOME)/develop/bin/ruff")

.PHONY: help install-dev lock test python-test ui-contracts js-check proxy-origin lint dev dev-down dev-logs

help:
	@printf '%s\n' \
	  'install-dev    Install resolver, sync, shared package, and lint dependencies' \
	  'lock           Refresh the uv-compiled resolver and sync dependency locks' \
	  'test           Run Python, browser-contract, JavaScript syntax, and proxy-origin checks' \
	  'python-test    Run the full Python suite and require TEST_DATABASE_URL' \
	  'ui-contracts   Run every tests/**/*.cjs contract' \
	  'js-check       Check syntax for every Admin v2 JavaScript file' \
	  'proxy-origin   Run the proxy-origin contract' \
	  'lint           Run Ruff correctness checks across the repository' \
	  'dev            Start the isolated hot-reload resolver and ephemeral test database' \
	  'dev-down       Stop the isolated development stack' \
	  'dev-logs       Follow the isolated development resolver logs'

install-dev:
	$(PYTHON) -m pip install -r requirements-dev.lock
	$(PYTHON) -m pip install --no-deps ./common

lock:
	uv pip compile --universal common/pyproject.toml resolver/requirements.txt --output-file resolver/requirements.lock
	uv pip compile --universal common/pyproject.toml sync/requirements.txt --output-file sync/requirements.lock

test: python-test ui-contracts js-check proxy-origin

python-test:
	ADULT_HUB_REQUIRE_TEST_DATABASE=1 ADULT_HUB_TEST_PROFILE=full CONFIG_PATH=tests/fixtures/config_telegram_only.yaml $(PYTHON) -m unittest discover -s tests -v

ui-contracts:
	@set -euo pipefail; \
	files="$$(mktemp)"; trap 'rm -f -- "$$files"' EXIT; \
	find tests -type f -name '*.cjs' -print0 | sort -z > "$$files"; \
	mapfile -d '' test_files < "$$files"; \
	(( $${#test_files[@]} > 0 )) || { echo 'No CommonJS browser contracts found' >&2; exit 1; }; \
	for test_file in "$${test_files[@]}"; do $(NODE) "$$test_file"; done

js-check:
	@set -euo pipefail; \
	files="$$(mktemp)"; trap 'rm -f -- "$$files"' EXIT; \
	find resolver/static/admin-v2 -type f -name '*.js' -print0 | sort -z > "$$files"; \
	mapfile -d '' script_files < "$$files"; \
	(( $${#script_files[@]} > 0 )) || { echo 'No Admin v2 JavaScript files found' >&2; exit 1; }; \
	for script_file in "$${script_files[@]}"; do $(NODE) --check "$$script_file"; done

proxy-origin:
	$(PYTHON) tests/proxy_origin_contract.py

lint:
	$(RUFF) check .

dev:
	docker compose -f docker-compose.dev.yml -p adult-hub-dev up --build -d

dev-down:
	docker compose -f docker-compose.dev.yml -p adult-hub-dev down

dev-logs:
	docker compose -f docker-compose.dev.yml -p adult-hub-dev logs -f resolver
