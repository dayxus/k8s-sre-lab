SHELL := /usr/bin/env bash

REPO_ROOT := $(patsubst %/,%,$(dir $(abspath $(lastword $(MAKEFILE_LIST)))))
TOOLS_DIR := $(REPO_ROOT)/.tools
PYTHON ?= $(shell if [ -x "$(REPO_ROOT)/.venv/bin/python" ]; then echo "$(REPO_ROOT)/.venv/bin/python"; else command -v python3; fi)
export PYTHON

.DEFAULT_GOAL := help

.PHONY: help setup venv tools test lint validate render audit check e2e clean

help: ## Show the available targets
	@grep -hE '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-10s\033[0m %s\n", $$1, $$2}'

venv: ## Create .venv and install the pinned dev dependencies
	python3 -m venv .venv
	./.venv/bin/pip install --upgrade pip
	./.venv/bin/pip install -r requirements-dev.txt

tools: ## Download the pinned validation binaries into .tools/
	./scripts/install-tools.sh all

setup: venv tools ## Create the venv and download the validation tooling

test: tools ## Run the pytest suite (manifest invariants, chart, app, hygiene)
	$(PYTHON) -m pytest -q

lint: tools ## ruff, shellcheck and the generated-docs check
	$(PYTHON) -m ruff check .
	$(PYTHON) -m ruff format --check .
	PATH="$(TOOLS_DIR):$$PATH" $(TOOLS_DIR)/shellcheck --severity=warning scripts/*.sh
	$(PYTHON) scripts/update_versions.py --check

validate: tools ## kustomize build + helm lint/template + kubeconform + kube-linter
	PYTHON=$(PYTHON) ./scripts/validate.sh

render: tools ## Render every deployment shape into build/
	$(PYTHON) scripts/render.py

audit: tools ## Write reports/weekly-audit.md (what the scheduled workflow runs)
	$(PYTHON) scripts/audit.py

check: lint test validate ## Full local gate: lint, tests, schema and policy validation

e2e: ## Run the kind E2E (needs docker, kind and kubectl; runs in CI)
	./scripts/e2e-kind.sh

clean: ## Remove rendered output, artifacts and caches
	rm -rf build artifacts .pytest_cache .ruff_cache
	find . -name __pycache__ -type d -prune -exec rm -rf {} +
