# Behave Configuration Reference

## behave.ini

Standard Behave configuration file. Place at project root.

```ini
[behave]
# Output
default_format = pretty
show_timings = true
color = true

# Default tag filter — skip WIP and explicitly skipped tests
default_tags = not @wip and not @skip

# Logging
logging_level = INFO
logging_format = %(asctime)s %(levelname)-8s %(name)s: %(message)s

# Capture control
stdout_capture = true
log_capture = true

# JUnit output (enable in CI)
junit = false
junit_directory = reports/

# Feature paths
paths = features/

[behave.userdata]
# Override with -D key=value on CLI
warehouse_id = auto
catalog = main
environment = dev
```

## pyproject.toml

Alternative configuration via pyproject.toml (Behave reads `[tool.behave]`):

**IMPORTANT:** In `pyproject.toml`, `default_tags` must be a **list**, not a string. The `behave.ini` parser accepts a plain string, but the TOML parser is stricter:

```toml
[tool.behave]
default_format = "pretty"
show_timings = true
default_tags = ["not @wip and not @skip"]  # MUST be a list in pyproject.toml
junit = false
junit_directory = "reports/"
logging_level = "INFO"

[tool.behave.userdata]
warehouse_id = "auto"
catalog = "main"
environment = "dev"
```

## Dependencies

Add to `pyproject.toml`:

```toml
[project.optional-dependencies]
test = [
    "behave>=1.2.6",
    "databricks-sdk>=0.40.0",
    "httpx>=0.27.0",
]

# Or for parallel execution
test-parallel = [
    "behave>=1.2.6",
    "behavex>=3.0",
    "databricks-sdk>=0.40.0",
    "httpx>=0.27.0",
]
```

With `uv`:

```bash
uv add --group test behave databricks-sdk httpx
```

## Makefile targets

```makefile
.PHONY: bdd bdd-smoke bdd-report bdd-rerun bdd-parallel bdd-dry-run

bdd:
	uv run behave --format=pretty --show-timings

bdd-smoke:
	uv run behave --tags="@smoke" --format=pretty

bdd-report:
	uv run behave --junit --junit-directory=reports/behave/ --format=progress

bdd-rerun:
	uv run behave @reports/rerun.txt

bdd-parallel:
	uv run behavex --parallel-processes 4 --parallel-scheme feature

bdd-dry-run:
	uv run behave --dry-run
```

## CI integration (GitHub Actions example)

```yaml
- name: Run BDD tests
  env:
    DATABRICKS_HOST: ${{ secrets.DATABRICKS_HOST }}
    DATABRICKS_TOKEN: ${{ secrets.DATABRICKS_TOKEN }}
    DATABRICKS_WAREHOUSE_ID: ${{ secrets.WAREHOUSE_ID }}
    TEST_CATALOG: ci_test
  run: |
    uv run behave \
      --tags="not @slow" \
      --junit --junit-directory=reports/behave/ \
      --format=progress \
      -D catalog=$TEST_CATALOG \
      -D warehouse_id=$DATABRICKS_WAREHOUSE_ID

- name: Upload test results
  if: always()
  uses: actions/upload-artifact@v4
  with:
    name: behave-results
    path: reports/behave/
```
