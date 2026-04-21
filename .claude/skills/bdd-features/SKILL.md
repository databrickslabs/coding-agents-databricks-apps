---
name: bdd-features
description: "This skill should be used when the user asks to \"write Gherkin\", \"create feature files\", \"generate BDD scenarios\", \"write acceptance tests in Gherkin\", \"create Behave features\", \"write Given When Then tests\", \"BDD test cases for my pipeline\", \"Gherkin for Unity Catalog\", or wants to translate requirements into Gherkin feature files for Databricks."
user-invocable: true
---

# BDD Features — Gherkin Feature File Generation

Generate well-structured Gherkin `.feature` files for Databricks workloads. Translate requirements, user stories, or existing code into behavior specifications using Given/When/Then syntax.

## When to use

- Translating requirements or user stories into Gherkin acceptance criteria
- Creating feature files for Databricks pipelines, catalog permissions, jobs, or Apps
- Writing regression tests in Gherkin for existing functionality
- Generating Scenario Outlines for data-driven testing

## Process

### 1. Identify the test subject

Determine what to test. Read the relevant code or ask the user:

- A Lakeflow SDP pipeline definition → pipeline behavior tests
- Unity Catalog grants/policies → permission verification tests
- A FastAPI Databricks App → API endpoint tests
- A notebook or job → execution and output validation tests
- SQL transformations → data quality and correctness tests

### 2. Write the feature file

Place feature files in the appropriate subdirectory under `features/`:

```
features/
├── catalog/permissions.feature
├── pipelines/events_pipeline.feature
├── apps/api_endpoints.feature
├── jobs/etl_notebook.feature
└── sql/data_quality.feature
```

**Structure every feature file with:**

1. **Tags** — `@domain`, `@smoke`/`@regression`/`@integration`, optional `@slow` or `@wip`
2. **Feature header** — name + As a / I want / So that narrative
3. **Background** — shared Given steps (workspace connection, test schema)
4. **Scenarios** — one behavior per scenario, descriptive names

Refer to `references/gherkin-patterns.md` for Databricks-specific Gherkin patterns covering:
- Pipeline lifecycle (full refresh, incremental, failure handling)
- Unity Catalog grants, column masks, row filters
- App endpoint testing with SSO headers
- Job/notebook execution and output validation
- SQL data quality assertions
- Scenario Outlines for parameterized testing

### 3. Gherkin writing principles

**Declarative, not imperative.** Describe *what* the system should do, not *how* to click buttons:

```gherkin
# Good — declarative
When I grant SELECT on "catalog.schema.table" to group "readers"
Then the group "readers" should have SELECT permission

# Bad — imperative
When I open the Catalog Explorer
And I click on the table "catalog.schema.table"
And I click "Permissions"
And I click "Grant"
And I select "SELECT"
And I type "readers" in the group field
And I click "Save"
```

**One behavior per scenario.** If a scenario tests two independent things, split it.

**Use Backgrounds for shared setup.** Avoid repeating connection/schema steps across scenarios.

**Scenario Outlines for data variations.** When the same behavior is tested with different inputs, use Examples tables instead of duplicating scenarios.

**Tag strategically:**
- `@smoke` — fast, critical-path tests (< 30 seconds each)
- `@regression` — thorough coverage (minutes)
- `@integration` — needs live workspace (skip in unit test CI)
- `@slow` — pipeline tests, job executions (> 2 minutes)

**CRITICAL — Curly braces break step matching.** Behave uses the `parse` library for step matching. `{anything}` in feature file text is interpreted as a capture group, not a literal. Never use `{test_schema}.table_name` in feature files — it will fail to match step definitions. Instead, use short table names (`"customers"`) and resolve the schema in step code.

**Trailing colons matter.** When a step has an attached data table or docstring, the `:` at the end of the Gherkin line IS part of the step text. The step pattern must include it: `@given('a table "{name}" with data:')` — not `with data` (no colon).

### 4. Validate step coverage

After writing features, check that step definitions exist for all steps:

```bash
uv run behave --dry-run
```

Any undefined steps will be reported with suggested snippets. Hand those to the `bdd-steps` skill for implementation.

## Additional resources

- **`references/gherkin-patterns.md`** — Complete Databricks Gherkin pattern library with examples for every domain
