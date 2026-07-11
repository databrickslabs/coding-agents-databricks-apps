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

# Omnigent host grants (see the grant-omnigent-host target). The server app
# hosts the *.databricksapps.com Omnigent URL; the wheel volume holds the
# omnigent CLI wheel. WHEEL_VOLUME (catalog.schema.volume) is derived from the
# LOCAL app.yaml being deployed (its OMNIGENTS_WHEEL_SPEC = /Volumes/cat/sch/vol)
# unless set explicitly. Reads the local file so it works before the first sync.
# GRANT_YAML lets the workshop/lakemeter targets point at their own variant.
GRANT_YAML ?= app.yaml
# Derive the server app name from OMNIGENTS_SERVER_URL in the deployed yaml
# (first hostname label) so the grant always targets the SAME server the app
# dials — no hardcoded per-user default. Override OMNIGENT_SERVER_APP to force.
OMNIGENT_SERVER_APP ?= $(shell python3 -c "import yaml,urllib.parse; e={v['name']:v.get('value','') for v in (yaml.safe_load(open('$(GRANT_YAML)')) or {}).get('env',[])}; h=(urllib.parse.urlparse(e.get('OMNIGENTS_SERVER_URL','')).hostname or '').split('.')[0]; base,_,wsid=h.rpartition('-'); print(base if wsid.isdigit() and base else h)" 2>/dev/null)
WHEEL_VOLUME ?= $(shell python3 -c "import sys,yaml; e={v['name']:v.get('value','') for v in (yaml.safe_load(open('$(GRANT_YAML)')) or {}).get('env',[])}; p=e.get('OMNIGENTS_WHEEL_SPEC','').strip('/').split('/'); print('.'.join(p[1:4]) if len(p)>=4 and p[0]=='Volumes' else '')" 2>/dev/null)

# ── Git-based deploy config ──────────────────────────
# Databricks Apps can deploy directly from a Git repo/ref (how coda-01..08 run).
# Override any of these on the command line.
GIT_URL      ?= https://github.com/dgokeeffe/coding-agents-databricks-apps-private
GIT_PROVIDER ?= gitHub
GIT_REF      ?= main
# GIT_REF_TYPE: branch | tag | commit
GIT_REF_TYPE ?= branch

.PHONY: help test integration-test e2e-test e2e-auth deploy redeploy create-app create-pat sync deploy-app status open clean enterprise-doctor \
	deploy-workshop redeploy-workshop guard-workshop-name create-app-workshop workshop-yaml workshop-secret \
	deploy-lakemeter redeploy-lakemeter lakemeter-yaml grant-omnigent-host \
	configure-git configure-git-credential deploy-git redeploy-git

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

deploy: create-app grant-omnigent-host sync deploy-app ## Full deploy (create app, grant Omnigent host IAM, sync, deploy)
	@echo ""
	@echo "Deployment complete! App URL:"
	@databricks apps get $(APP_NAME) --profile $(PROFILE) --output json 2>/dev/null | python3 -c "import sys,json; print(json.load(sys.stdin).get('url','(pending)'))"

redeploy: grant-omnigent-host sync deploy-app ## Redeploy: (re)grant Omnigent host IAM + sync + deploy
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

deploy-workshop: GRANT_YAML=app.yaml.workshop
deploy-workshop: guard-workshop-name create-app-workshop grant-omnigent-host sync workshop-yaml deploy-app ## Deploy a workshop instance (LARGE + app.yaml.workshop)
	@echo ""
	@echo "Workshop deployment complete! App URL:"
	@databricks apps get $(APP_NAME) --profile $(PROFILE) --output json 2>/dev/null | python3 -c "import sys,json; print(json.load(sys.stdin).get('url','(pending)'))"

redeploy-workshop: GRANT_YAML=app.yaml.workshop
redeploy-workshop: guard-workshop-name grant-omnigent-host sync workshop-yaml deploy-app ## Redeploy a workshop instance (skip app creation)
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

# ── Lakemeter deploy (Omnigent host ON) ──────────────
# The committed app.yaml keeps Omnigent OFF/commented for the upstream PR.
# These targets swap app.yaml.lakemeter in as the deployed app.yaml so the
# lakemeter app self-registers as an always-on Omnigent host, without
# re-poisoning the committed config.
#   make deploy-lakemeter PROFILE=lakemeter
#   make redeploy-lakemeter PROFILE=lakemeter

deploy-lakemeter: GRANT_YAML=$(LAKEMETER_YAML)
deploy-lakemeter: create-app grant-omnigent-host sync lakemeter-yaml deploy-app ## Deploy to lakemeter (app.yaml.lakemeter, Omnigent host ON)
	@echo ""
	@echo "Lakemeter deployment complete! App URL:"
	@databricks apps get $(APP_NAME) --profile $(PROFILE) --output json 2>/dev/null | python3 -c "import sys,json; print(json.load(sys.stdin).get('url','(pending)'))"

redeploy-lakemeter: GRANT_YAML=$(LAKEMETER_YAML)
redeploy-lakemeter: grant-omnigent-host sync lakemeter-yaml deploy-app ## Redeploy to lakemeter (skip app creation)
	@echo ""
	@echo "Lakemeter redeployment complete!"

# Prefer the git-ignored .local override (real workspace values) over the
# committed template (which has <placeholders>). Deploying the template would
# dial <your-omnigent-app> and fail — so require .local to exist.
LAKEMETER_YAML := $(shell [ -f app.yaml.lakemeter.local ] && echo app.yaml.lakemeter.local || echo app.yaml.lakemeter)

lakemeter-yaml: ## Overwrite the synced app.yaml with the lakemeter variant (.local preferred)
	@if [ "$(LAKEMETER_YAML)" = "app.yaml.lakemeter" ]; then \
		echo "ERROR: app.yaml.lakemeter.local not found — the committed template has"; \
		echo "       <placeholders>, not real values. Copy app.yaml.lakemeter to"; \
		echo "       app.yaml.lakemeter.local and fill in your OMNIGENTS_SERVER_URL /"; \
		echo "       OMNIGENTS_WHEEL_SPEC before deploying."; \
		exit 1; \
	fi
	@echo "==> Swapping in $(LAKEMETER_YAML) as $(WORKSPACE_PATH)/app.yaml..."
	@databricks workspace import $(WORKSPACE_PATH)/app.yaml --file $(LAKEMETER_YAML) --format AUTO --overwrite --profile $(PROFILE)

# ── Git-based deploy (Databricks Apps native Git) ────
# Deploys straight from a Git ref instead of the sync-to-workspace path.
#   make configure-git APP_NAME=coda-04 PROFILE=daveok
#   gh auth token | make configure-git-credential APP_NAME=coda-04 PROFILE=daveok
#   make deploy-git APP_NAME=coda-04 PROFILE=daveok GIT_REF=main

configure-git: create-app ## Attach the Git repo ($(GIT_URL)) to the app (idempotent)
	@echo "==> Configuring Git repo on '$(APP_NAME)' ($(GIT_URL))..."
	@databricks apps create-update $(APP_NAME) --profile $(PROFILE) \
		--json '{"update_mask":"git_repository","git_repository":{"url":"$(GIT_URL)","provider":"$(GIT_PROVIDER)"}}' >/dev/null \
		&& echo "    Git repo attached." \
		|| { echo "    create-update failed (older CLI/app?); ensure the repo is set via the UI."; exit 1; }

configure-git-credential: ## Add a Git credential to the app SP for private repos (reads token from stdin)
	@# Usage: gh auth token | make configure-git-credential APP_NAME=coda-04 PROFILE=daveok
	@# Requires CAN MANAGE on the app SP. Token is read from stdin so it never
	@# lands on a command line or in shell history.
	@sp_id=$$(databricks apps get $(APP_NAME) --profile $(PROFILE) --output json 2>/dev/null \
		| python3 -c "import sys,json; print(json.load(sys.stdin).get('service_principal_id',''))"); \
	if [ -z "$$sp_id" ]; then echo "ERROR: could not resolve SP id for '$(APP_NAME)'" >&2; exit 1; fi; \
	token=$$(cat); \
	printf '%s' "$$token" | python3 -c "import sys,json; t=sys.stdin.read().strip(); print(json.dumps({'git_provider':'$(GIT_PROVIDER)','git_email':'$(USER_EMAIL)','personal_access_token':t,'principal_id':int('$$sp_id'),'name':'Git credential for $(APP_NAME) SP'}))" \
		| databricks git-credentials create --profile $(PROFILE) --json @/dev/stdin >/dev/null \
		&& echo "    Git credential added to SP $$sp_id for '$(APP_NAME)'." \
		|| echo "    git-credentials create failed (credential may already exist for $(GIT_PROVIDER))."

deploy-git: ## Deploy the app from the configured Git ref ($(GIT_REF_TYPE)=$(GIT_REF))
	@echo "==> Deploying '$(APP_NAME)' from Git $(GIT_REF_TYPE)='$(GIT_REF)'..."
	@databricks apps deploy $(APP_NAME) --profile $(PROFILE) --no-wait \
		--json '{"git_source":{"$(GIT_REF_TYPE)":"$(GIT_REF)"}}'
	@echo "    Deploy submitted. App URL:"
	@databricks apps get $(APP_NAME) --profile $(PROFILE) --output json 2>/dev/null | python3 -c "import sys,json; print(json.load(sys.stdin).get('url','(pending)'))"

redeploy-git: grant-omnigent-host deploy-git ## (Re)grant Omnigent host IAM, then deploy from Git
	@echo ""
	@echo "Git redeployment complete!"

# ── Monitoring ───────────────────────────────────────

grant-omnigent-host: ## Grant the CoDA app SP the IAM to register as an Omnigent host (CAN_USE + USE_CATALOG/USE_SCHEMA + READ/WRITE_VOLUME)
	@# A deployed CoDA app registers as an Omnigent host by running
	@# `omnigent host <server>` as its own service principal. That SP starts with
	@# ZERO privileges, so it needs two one-time grants or the host tunnel 302s at
	@# /v1/me and never appears in the Omnigent picker:
	@#   1. CAN_USE on the Omnigent server app  (so the Apps edge accepts its token)
	@#   2. The FULL UC traversal chain on the wheel volume so it can install the
	@#      omnigent CLI: USE_CATALOG -> USE_SCHEMA -> READ_VOLUME/WRITE_VOLUME.
	@#      READ_VOLUME alone is a silent trap ("User does not have USE CATALOG").
	@#      See docs/deployment.md and docs/plans/2026-07-11-omnigent-host-deploy-*.
	@# grant_omnigent_host.sh does both idempotently. OMNIGENT_SERVER_APP defaults
	@# to "omnigent"; WHEEL_VOLUME is derived from the deployed app.yaml's
	@# OMNIGENTS_WHEEL_SPEC unless overridden. See the vars above 'status:'.
	@if [ -z "$(WHEEL_VOLUME)" ]; then \
		echo "==> Omnigent host not enabled in $(GRANT_YAML) (no OMNIGENTS_WHEEL_SPEC) - skipping grant."; \
		echo "    (Pass WHEEL_VOLUME=<catalog>.<schema>.<volume> to force it.)"; \
	else \
		echo "==> Granting Omnigent host IAM to '$(APP_NAME)' SP (server='$(OMNIGENT_SERVER_APP)', volume='$(WHEEL_VOLUME)')..."; \
		./grant_omnigent_host.sh \
			--profile $(PROFILE) \
			--coda-app $(APP_NAME) \
			--server-app $(OMNIGENT_SERVER_APP) \
			--wheel-volume $(WHEEL_VOLUME); \
	fi

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

