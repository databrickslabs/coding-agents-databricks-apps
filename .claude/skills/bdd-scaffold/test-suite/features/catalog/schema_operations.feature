@catalog @smoke
Feature: Unity Catalog schema and table operations
  As a data engineer
  I want to verify Unity Catalog operations work correctly
  So that I can manage my data assets with confidence

  Background:
    Given a Databricks workspace connection is established
    And a test schema is provisioned

  Scenario: Ephemeral test schema was created
    Then the test schema should exist in Unity Catalog

  Scenario: Create tables and list them
    Given a managed table "table_alpha" exists
    And a managed table "table_beta" exists
    When I list tables in the test schema
    Then the table list should include "table_alpha"
    And the table list should include "table_beta"

  Scenario: Table with data is queryable via SQL
    Given a managed table "products" with data:
      | product_id | name      | price |
      | 1          | Widget    | 9.99  |
      | 2          | Gadget    | 19.99 |
      | 3          | Doohickey | 4.99  |
    Then the managed table "products" should have 3 rows
    When I execute a query on the test schema:
      """
      SELECT name, price FROM {schema}.products WHERE CAST(price AS DOUBLE) > 10.0
      """
    Then the query result should have 1 rows
    And the first result column "name" should be "Gadget"

  Scenario: Grant SELECT permission on a table
    Given a managed table "grant_test" exists
    When I grant SELECT on managed table "grant_test" to group "users"
    Then the group "users" should have SELECT on managed table "grant_test"
