SHELL := /bin/bash
.DEFAULT_GOAL := help

# proxy-origin and the Python suite import the application dependencies, which
# CI installs into the PATH interpreter but a local checkout usually keeps in
# the repository .venv. When the PATH interpreter cannot import them and a
# .venv exists, use it, so the checks do not fail on imports alone.
PYTHON ?= $(shell if [ -x .venv/bin/python ] && ! python3 -c 'import uvicorn, telethon' 2>/dev/null; then echo .venv/bin/python; else echo python3; fi)
NODE ?= node
COVERAGE ?= $(shell command -v coverage 2>/dev/null || (test -x .venv/bin/coverage && echo .venv/bin/coverage) || printf '%s' "$(PYTHON) -m coverage")
RUFF ?= $(shell command -v ruff 2>/dev/null || (test -x .venv/bin/ruff && echo .venv/bin/ruff) || printf '%s' "$(HOME)/develop/bin/ruff")

# Release images are tagged from VERSION, so a version bump has one place to
# edit and `make deploy` can never build an image tagged with an old release.
APP_VERSION := $(shell tr -d '[:space:]' < VERSION)
export APP_VERSION

# Shared environment for the database-backed Python suite: the isolated test
# profile plus a fixture config, so no production credentials are consulted.
# The Telegram transfer-budget ledger also needs a writable path; /data only
# exists on the deployment host and one process per run keeps the daily
# counters from leaking between concurrent suites.
TEST_ENV = ADULT_HUB_REQUIRE_TEST_DATABASE=1 ADULT_HUB_TEST_PROFILE=full CONFIG_PATH=tests/fixtures/config_telegram_only.yaml TELEGRAM_FINGERPRINT_USAGE_PATH=/tmp/adulthub-test-fingerprint-usage-$$.json

.PHONY: help install-dev lock test python-test coverage ui-contracts js-check proxy-origin lint deploy dev dev-down dev-logs setup update package-release

help:
	@printf '%s\n' \
	  'install-dev    Install resolver, sync, shared package, and lint dependencies' \
	  'lock           Refresh the uv-compiled resolver and sync dependency locks' \
	  'test           Run Python, browser-contract, JavaScript syntax, and proxy-origin checks' \
	  'python-test    Run the full Python suite and require TEST_DATABASE_URL' \
	  'coverage       Run the same suite while reporting test coverage' \
	  'ui-contracts   Run every tests/**/*.cjs contract' \
	  'js-check       Check syntax for every Admin v2 JavaScript file' \
	  'proxy-origin   Run the proxy-origin contract' \
	  'lint           Run Ruff correctness checks across the repository' \
	  'setup          Run the interactive standalone setup installer' \
	  'update         Update standalone services, run migrations, and verify readiness' \
	  'package-release Package standalone prebuilt Docker Compose release bundle' \
	  'deploy         Build and restart the release stack with the VERSION image tag' \
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
	$(TEST_ENV) $(PYTHON) -m unittest discover -s tests -v

coverage:
	$(TEST_ENV) $(COVERAGE) run -m unittest discover -s tests
	$(COVERAGE) report -m --fail-under=72

ui-contracts:
	@set -euo pipefail; \
	files="$$(mktemp)"; trap 'rm -f -- "$$files"' EXIT; \
	find tests -type f -name '*.cjs' -not -path 'tests/helpers/*' -print0 | sort -z > "$$files"; \
	mapfile -d '' test_files < "$$files"; \
	(( $${#test_files[@]} > 0 )) || { echo 'No CommonJS browser contracts found' >&2; exit 1; }; \
	for test_file in "$${test_files[@]}"; do \
	  if ! output="$$( $(NODE) "$$test_file" 2>&1 )"; then \
	    printf '%s\n' "$$output" >&2; echo "FAIL: $$test_file" >&2; exit 1; \
	  fi; \
	  printf '%s\n' "$$output"; \
	done

js-check:
	@set -euo pipefail; \
	files="$$(mktemp)"; trap 'rm -f -- "$$files"' EXIT; \
	find resolver/static/admin-v2 -type f -name '*.js' -print0 | sort -z > "$$files"; \
	mapfile -d '' script_files < "$$files"; \
	(( $${#script_files[@]} > 0 )) || { echo 'No Admin v2 JavaScript files found' >&2; exit 1; }; \
	for script_file in "$${script_files[@]}"; do $(NODE) --check "$$script_file"; done

js-lint:
	npx --no-install eslint resolver/static/admin-v2

proxy-origin:
	$(PYTHON) -m tests.proxy_origin_contract

lint:
	$(RUFF) check .
	$(MAKE) js-lint

setup:
	@scripts/setup.sh

update:
	@scripts/update.sh

package-release:
	@scripts/package_release.sh '$(APP_VERSION)'

# Application services only: Jellyfin is defined in the same file and is not
# part of a release, so it stays out of a deploy.
deploy:
	@printf 'Deploying Adult Hub %s\n' '$(APP_VERSION)'
	docker compose build resolver sync
	docker compose up -d --no-build resolver sync

dev:
	docker compose -f docker-compose.dev.yml -p adult-hub-dev up --build -d

dev-down:
	docker compose -f docker-compose.dev.yml -p adult-hub-dev down

dev-logs:
	docker compose -f docker-compose.dev.yml -p adult-hub-dev logs -f resolver
