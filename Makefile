# Makefile for deploying Coding Agents to Databricks Apps
#
# Usage:
#   make deploy PROFILE=dogfood              # full deploy (create app, sync, deploy)
#   make redeploy PROFILE=dogfood            # skip app creation, just sync + deploy
#   make create-pat PROFILE=dogfood          # generate a 1-day PAT and copy to clipboard
#   make status PROFILE=dogfood              # check app status
#   make open PROFILE=dogfood                # open app in browser
#   make clean PROFILE=dogfood               # remove app and secret scope

# Configuration (accepts lowercase: make deploy profile=dogfood)
ifdef profile
PROFILE := $(profile)
endif
ifdef app_name
APP_NAME := $(app_name)
endif
PROFILE       ?= DEFAULT
APP_NAME      ?= coding-agents

# Resolve user email and workspace path from the profile
USER_EMAIL    = $(shell databricks current-user me --profile $(PROFILE) --output json 2>/dev/null | python3 -c "import sys,json; print(json.load(sys.stdin).get('userName',''))")
WORKSPACE_PATH = /Workspace/Users/$(USER_EMAIL)/apps/$(APP_NAME)

.PHONY: help test integration-test e2e-test e2e-auth deploy redeploy create-app create-pat sync deploy-app status open clean enterprise-doctor

# ── Help ─────────────────────────────────────────────

test: ## Run unit tests (fast — excludes Docker integration + Playwright e2e)
	uv run pytest tests/ -v --ignore=tests/integration --ignore=tests/e2e

integration-test: ## Run Docker-based pipeline integration test (~3-5 min wall time)
	uv run pytest tests/integration/ -v -s -rs

e2e-test: ## Run Playwright e2e against live deployed app (needs `make e2e-auth` first)
	uv run pytest tests/e2e/ -v -s

e2e-auth: ## Record SSO session for e2e tests (one-time per cookie expiry)
	@# Resolve the app URL via the configured profile, then launch a headed
	@# Chromium that saves storage state to tests/e2e/auth.json.
	@url=$$(databricks apps get coding-agents --profile $(PROFILE) --output json 2>/dev/null \
		| python3 -c "import sys,json; print(json.load(sys.stdin)['url'])") && \
	echo "Recording SSO session against $$url ..." && \
	uv run playwright codegen --save-storage tests/e2e/auth.json "$$url"
	@echo ""
	@echo "Auth state saved to tests/e2e/auth.json (gitignored)."
	@echo "Run `make e2e-test PROFILE=$(PROFILE)` to execute the suite."

help: ## Show this help
	@grep -E '^[a-zA-Z0-9_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-18s\033[0m %s\n", $$1, $$2}'

# ── Workflows ────────────────────────────────────────

deploy: create-app sync deploy-app ## Full deploy (create app, sync, deploy)
	@echo ""
	@echo "Deployment complete! App URL:"
	@databricks apps get $(APP_NAME) --profile $(PROFILE) --output json 2>/dev/null | python3 -c "import sys,json; print(json.load(sys.stdin).get('url','(pending)'))"

redeploy: sync deploy-app ## Redeploy: sync + deploy (skip secret setup)
	@echo ""
	@echo "Redeployment complete!"

# ── Building Blocks ──────────────────────────────────

create-app: ## Create the Databricks App (idempotent)
	@echo "==> Checking if app '$(APP_NAME)' exists..."
	@state=$$(databricks apps get $(APP_NAME) --profile $(PROFILE) --output json 2>/dev/null \
		| python3 -c "import sys,json; print(json.load(sys.stdin).get('compute_status',{}).get('state',''))" 2>/dev/null); \
	if [ "$$state" = "DELETING" ]; then \
		echo "    App '$(APP_NAME)' is still deleting, waiting..."; \
		while [ "$$state" = "DELETING" ]; do \
			sleep 10; \
			state=$$(databricks apps get $(APP_NAME) --profile $(PROFILE) --output json 2>/dev/null \
				| python3 -c "import sys,json; print(json.load(sys.stdin).get('compute_status',{}).get('state',''))" 2>/dev/null); \
		done; \
		echo "    Deletion complete."; \
		echo "    Creating app '$(APP_NAME)'..."; \
		databricks apps create $(APP_NAME) --profile $(PROFILE); \
	elif [ -n "$$state" ]; then \
		echo "    App '$(APP_NAME)' already exists (state: $$state), skipping create."; \
	else \
		echo "    Creating app '$(APP_NAME)'..."; \
		databricks apps create $(APP_NAME) --profile $(PROFILE); \
	fi

create-pat: ## Generate a 1-day PAT and copy it to your clipboard
	@echo "==> Generating a 1-day PAT..."
	@token=$$(databricks tokens create --lifetime-seconds $$((1 * 24 * 60 * 60)) --comment "coding-agents (1-day)" --profile $(PROFILE) --output json \
		| python3 -c "import sys,json; print(json.load(sys.stdin)['token_value'])") && \
	echo "$$token" | pbcopy && \
	echo "    PAT copied to clipboard! (expires in 24 hours)"


sync: ## Sync local files to Databricks workspace
	@echo "==> Syncing to $(WORKSPACE_PATH)..."
	@databricks sync . $(WORKSPACE_PATH) --watch=false --profile $(PROFILE)

deploy-app: ## Deploy the app from workspace
	@echo "==> Deploying app '$(APP_NAME)'..."
	@databricks apps deploy $(APP_NAME) --source-code-path $(WORKSPACE_PATH) --profile $(PROFILE) --no-wait

# ── Monitoring ───────────────────────────────────────

status: ## Check app status
	@databricks apps get $(APP_NAME) --profile $(PROFILE)

open: ## Open the app in browser
	@databricks apps get $(APP_NAME) --profile $(PROFILE) --output json 2>/dev/null \
		| python3 -c "import sys,json; print(json.load(sys.stdin).get('url',''))" \
		| xargs open

# ── Enterprise mode ─────────────────────────────────

enterprise-doctor: ## Probe configured enterprise mirrors (PyPI, npm, GitHub) for reachability
	@# Use the existing venv directly so the doctor doesn't itself trigger a uv resolve
	@# (which would fail if PyPI is firewalled — the exact scenario this target diagnoses).
	@if [ -x .venv/bin/python ]; then \
		.venv/bin/python scripts/enterprise_doctor.py; \
	else \
		uv run python scripts/enterprise_doctor.py; \
	fi

# ── Cleanup (destructive) ───────────────────────────

clean: ## Remove the app (destructive)
	@echo "==> Removing app '$(APP_NAME)'..."
	@databricks apps delete $(APP_NAME) --profile $(PROFILE) 2>/dev/null && \
		echo "    App '$(APP_NAME)' deleted." || \
		echo "    App '$(APP_NAME)' not found or already deleted."

