# Makefile for deploying Coding Agents to Databricks Apps
#
# Usage:
#   make deploy-e2e PROFILE=dogfood          # fully automated deploy (auto-generates PAT)
#   make deploy PROFILE=dogfood              # full deploy (prompts for PAT interactively)
#   make redeploy PROFILE=dogfood            # skip secret setup, just sync + deploy
#   make status PROFILE=dogfood              # check app status
#   make open PROFILE=dogfood                # open app in browser
#   make clean PROFILE=dogfood               # remove app and secret scope

# Configuration (accepts lowercase: make deploy-e2e profile=dogfood)
ifdef profile
PROFILE := $(profile)
endif
ifdef app_name
APP_NAME := $(app_name)
endif
ifdef pat
PAT := $(pat)
endif
PROFILE       ?= DEFAULT
APP_NAME      ?= coding-agents
SECRET_SCOPE  ?= $(APP_NAME)-secrets
SECRET_KEY    ?= databricks-token

# Resolve user email and workspace path from the profile
USER_EMAIL    = $(shell databricks current-user me --profile $(PROFILE) --output json 2>/dev/null | python3 -c "import sys,json; print(json.load(sys.stdin).get('userName',''))")
WORKSPACE_PATH = /Workspace/Users/$(USER_EMAIL)/apps/$(APP_NAME)

.PHONY: help deploy-e2e deploy redeploy create-app create-pat setup-secret sync deploy-app status open clean clean-secret

# ── Help ─────────────────────────────────────────────

help: ## Show this help
	@grep -E '^[a-zA-Z0-9_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-18s\033[0m %s\n", $$1, $$2}'

# ── Workflows ────────────────────────────────────────

deploy-e2e: create-app create-pat sync deploy-app ## Full automated deploy (auto-generates PAT)
	@echo ""
	@echo "Deployment complete! App URL:"
	@databricks apps get $(APP_NAME) --profile $(PROFILE) --output json 2>/dev/null | python3 -c "import sys,json; print(json.load(sys.stdin).get('url','(pending)'))"

deploy: create-app setup-secret sync deploy-app ## Full deploy (prompts for PAT interactively)
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

create-pat: ## Generate a 90-day PAT and store it as the app secret
	@echo "==> Ensuring secret scope '$(SECRET_SCOPE)' exists..."
	@if databricks secrets list-scopes --profile $(PROFILE) --output json 2>/dev/null | python3 -c "import sys,json; d=json.load(sys.stdin); scopes=[s['name'] for s in (d if isinstance(d,list) else d.get('scopes',[]))]; exit(0 if '$(SECRET_SCOPE)' in scopes else 1)" 2>/dev/null; then \
		echo "    Secret scope '$(SECRET_SCOPE)' already exists."; \
	else \
		echo "    Creating secret scope '$(SECRET_SCOPE)'..."; \
		databricks secrets create-scope $(SECRET_SCOPE) --profile $(PROFILE); \
	fi
	@echo "==> Generating a 90-day PAT..."
	@databricks tokens create --lifetime-seconds $$((90 * 24 * 60 * 60)) --comment "coding-agents (auto-generated)" --profile $(PROFILE) --output json \
		| python3 -c "import sys,json; print(json.load(sys.stdin)['token_value'])" \
		| databricks secrets put-secret $(SECRET_SCOPE) $(SECRET_KEY) --profile $(PROFILE)
	@echo "    PAT created and stored in $(SECRET_SCOPE)/$(SECRET_KEY)"
	@echo "==> Linking secret to app resource 'DATABRICKS_TOKEN'..."
	@curl -s -X PATCH \
		"$$(databricks auth env --profile $(PROFILE) 2>/dev/null | python3 -c "import sys,json; print(json.load(sys.stdin)['env']['DATABRICKS_HOST'])")/api/2.0/apps/$(APP_NAME)" \
		-H "Authorization: Bearer $$(databricks auth token --profile $(PROFILE) 2>/dev/null | python3 -c "import sys,json; print(json.load(sys.stdin)['access_token'])")" \
		-H "Content-Type: application/json" \
		-d '{"resources":[{"name":"DATABRICKS_TOKEN","description":"PAT for model serving access","secret":{"scope":"$(SECRET_SCOPE)","key":"$(SECRET_KEY)","permission":"READ"}}]}' \
		>/dev/null
	@echo "    App resource linked."

setup-secret: ## Create secret scope and store PAT (interactive)
	@echo "==> Setting up DATABRICKS_TOKEN secret..."
	@# Create scope if it doesn't exist
	@if databricks secrets list-scopes --profile $(PROFILE) --output json 2>/dev/null | python3 -c "import sys,json; d=json.load(sys.stdin); scopes=[s['name'] for s in (d if isinstance(d,list) else d.get('scopes',[]))]; exit(0 if '$(SECRET_SCOPE)' in scopes else 1)" 2>/dev/null; then \
		echo "    Secret scope '$(SECRET_SCOPE)' already exists."; \
	else \
		echo "    Creating secret scope '$(SECRET_SCOPE)'..."; \
		databricks secrets create-scope $(SECRET_SCOPE) --profile $(PROFILE); \
	fi
	@# Store the PAT - prompt if not provided
	@if [ -z "$(PAT)" ]; then \
		echo "    Enter your Databricks PAT (will not echo):"; \
		read -s pat_value && \
		echo "$$pat_value" | databricks secrets put-secret $(SECRET_SCOPE) $(SECRET_KEY) --profile $(PROFILE); \
	else \
		echo "$(PAT)" | databricks secrets put-secret $(SECRET_SCOPE) $(SECRET_KEY) --profile $(PROFILE); \
	fi
	@echo "    Secret stored in $(SECRET_SCOPE)/$(SECRET_KEY)"
	@# Link secret to app resource
	@echo "    Linking secret to app resource 'DATABRICKS_TOKEN'..."
	@curl -s -X PATCH \
		"$$(databricks auth env --profile $(PROFILE) 2>/dev/null | python3 -c "import sys,json; print(json.load(sys.stdin)['env']['DATABRICKS_HOST'])")/api/2.0/apps/$(APP_NAME)" \
		-H "Authorization: Bearer $$(databricks auth token --profile $(PROFILE) 2>/dev/null | python3 -c "import sys,json; print(json.load(sys.stdin)['access_token'])")" \
		-H "Content-Type: application/json" \
		-d '{"resources":[{"name":"DATABRICKS_TOKEN","description":"PAT for model serving access","secret":{"scope":"$(SECRET_SCOPE)","key":"$(SECRET_KEY)","permission":"READ"}}]}' \
		>/dev/null
	@echo "    App resource linked."

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

# ── Cleanup (destructive) ───────────────────────────

clean: ## Remove app and secret scope (destructive)
	@echo "==> Removing app '$(APP_NAME)'..."
	@databricks apps delete $(APP_NAME) --profile $(PROFILE) 2>/dev/null && \
		echo "    App '$(APP_NAME)' deleted." || \
		echo "    App '$(APP_NAME)' not found or already deleted."
	@echo "==> Removing secret scope '$(SECRET_SCOPE)'..."
	@databricks secrets delete-scope $(SECRET_SCOPE) --profile $(PROFILE) 2>/dev/null && \
		echo "    Secret scope '$(SECRET_SCOPE)' deleted." || \
		echo "    Secret scope '$(SECRET_SCOPE)' not found or already deleted."

clean-secret: ## Remove secret and optionally the scope (destructive)
	@echo "==> Removing secret '$(SECRET_KEY)' from scope '$(SECRET_SCOPE)'..."
	@databricks secrets delete-secret $(SECRET_SCOPE) $(SECRET_KEY) --profile $(PROFILE) 2>/dev/null && \
		echo "    Secret '$(SECRET_KEY)' deleted." || \
		echo "    Secret '$(SECRET_KEY)' not found or already deleted."
	@printf "    Remove the entire secret scope '$(SECRET_SCOPE)'? [y/N] " && \
		read answer && \
		if [ "$$answer" = "y" ] || [ "$$answer" = "Y" ]; then \
			echo "    Removing secret scope '$(SECRET_SCOPE)'..."; \
			databricks secrets delete-scope $(SECRET_SCOPE) --profile $(PROFILE) && \
				echo "    Secret scope '$(SECRET_SCOPE)' deleted." || \
				echo "    Failed to delete secret scope."; \
		else \
			echo "    Keeping secret scope '$(SECRET_SCOPE)'."; \
		fi
