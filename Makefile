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

.PHONY: help test deploy redeploy create-app create-pat sync deploy-app status open clean \
	deploy-workshop redeploy-workshop guard-workshop-name create-app-workshop workshop-yaml workshop-secret

# ── Help ─────────────────────────────────────────────

test: ## Run unit tests
	uv run pytest tests/ -v

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

# ── Workshop (spec-A M1) ─────────────────────────────
# Usage: make deploy-workshop PROFILE=lakemeter APP_NAME=coding-agents-01
# Creates the app at LARGE compute and swaps app.yaml.workshop in as the
# deployed app.yaml (host-register OFF, 10 sessions, preloaded challenge repo).

WS_COMPUTE_SIZE ?= LARGE
WS_SECRET_SCOPE ?= coda-workshop
WS_SECRET_KEY   ?= challenge-repo-read-token

deploy-workshop: guard-workshop-name create-app-workshop sync workshop-yaml deploy-app ## Deploy a workshop instance (LARGE + app.yaml.workshop)
	@echo ""
	@echo "Workshop deployment complete! App URL:"
	@databricks apps get $(APP_NAME) --profile $(PROFILE) --output json 2>/dev/null | python3 -c "import sys,json; print(json.load(sys.stdin).get('url','(pending)'))"

redeploy-workshop: guard-workshop-name sync workshop-yaml deploy-app ## Redeploy a workshop instance (skip app creation)
	@echo ""
	@echo "Workshop redeployment complete!"

guard-workshop-name: ## Refuse to deploy the workshop config over the main instance
	@if [ "$(APP_NAME)" = "coding-agents" ]; then \
		echo "ERROR: workshop deploy would overwrite the main 'coding-agents' app."; \
		echo "       Pass APP_NAME=coding-agents-01 (or -02..-06)."; \
		exit 1; \
	fi

create-app-workshop: ## Create the workshop app at $(WS_COMPUTE_SIZE) compute (idempotent)
	@echo "==> Checking if app '$(APP_NAME)' exists..."
	@state=$$(databricks apps get $(APP_NAME) --profile $(PROFILE) --output json 2>/dev/null \
		| python3 -c "import sys,json; print(json.load(sys.stdin).get('compute_status',{}).get('state',''))" 2>/dev/null); \
	if [ -n "$$state" ]; then \
		echo "    App '$(APP_NAME)' already exists (state: $$state), skipping create."; \
	else \
		echo "    Creating app '$(APP_NAME)' (compute size: $(WS_COMPUTE_SIZE))..."; \
		databricks apps create $(APP_NAME) --compute-size $(WS_COMPUTE_SIZE) --profile $(PROFILE); \
	fi

workshop-yaml: ## Overwrite the synced app.yaml with the workshop variant
	@echo "==> Swapping in app.yaml.workshop as $(WORKSPACE_PATH)/app.yaml..."
	@databricks workspace import $(WORKSPACE_PATH)/app.yaml --file app.yaml.workshop --format AUTO --overwrite --profile $(PROFILE)

# Reads the token from stdin so it never appears on a command line:
#   gh auth token | make workshop-secret PROFILE=lakemeter APP_NAME=coding-agents-01
# NOTE: `apps update` REPLACES the app's resources list — workshop apps have
# only this one resource, so a plain write is correct here.
workshop-secret: guard-workshop-name ## Store the challenge-repo read token (from stdin) and attach it to the app
	@databricks secrets create-scope $(WS_SECRET_SCOPE) --profile $(PROFILE) 2>/dev/null || true
	@printf '%s' "$$(cat)" | databricks secrets put-secret $(WS_SECRET_SCOPE) $(WS_SECRET_KEY) --profile $(PROFILE)
	@databricks apps update $(APP_NAME) --profile $(PROFILE) --json '{"resources": [{"name": "challenge-repo-token", "secret": {"scope": "$(WS_SECRET_SCOPE)", "key": "$(WS_SECRET_KEY)", "permission": "READ"}}]}' > /dev/null
	@echo "    Secret stored ($(WS_SECRET_SCOPE)/$(WS_SECRET_KEY)) and attached as app resource 'challenge-repo-token'."
	@echo "    Redeploy the app for the env var to take effect."

# ── Monitoring ───────────────────────────────────────

status: ## Check app status
	@databricks apps get $(APP_NAME) --profile $(PROFILE)

open: ## Open the app in browser
	@databricks apps get $(APP_NAME) --profile $(PROFILE) --output json 2>/dev/null \
		| python3 -c "import sys,json; print(json.load(sys.stdin).get('url',''))" \
		| xargs open

# ── Cleanup (destructive) ───────────────────────────

clean: ## Remove the app (destructive)
	@echo "==> Removing app '$(APP_NAME)'..."
	@databricks apps delete $(APP_NAME) --profile $(PROFILE) 2>/dev/null && \
		echo "    App '$(APP_NAME)' deleted." || \
		echo "    App '$(APP_NAME)' not found or already deleted."

