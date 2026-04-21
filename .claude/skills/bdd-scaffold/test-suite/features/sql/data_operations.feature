@sql @smoke
Feature: SQL data operations via Databricks
  As a data engineer
  I want to verify SQL operations work correctly against the warehouse
  So that I can trust my data transformations

  Background:
    Given a Databricks workspace connection is established
    And a test schema is provisioned

  Scenario: Create a table and verify it exists
    Given a managed table "smoke_test" exists
    Then the managed table "smoke_test" should exist

  Scenario: Insert and count rows
    Given a managed table "customers" with data:
      | customer_id | name    | email               |
      | 1           | Alice   | alice@example.com   |
      | 2           | Bob     | bob@example.com     |
      | 3           | Charlie | charlie@example.com |
    Then the managed table "customers" should have 3 rows

  Scenario: Aggregate query returns correct results
    Given a managed table "orders" with data:
      | order_id | customer_id | amount |
      | 101      | 1           | 50     |
      | 102      | 1           | 75     |
      | 103      | 2           | 100    |
    When I execute a query on the test schema:
      """
      SELECT customer_id, COUNT(*) as order_count
      FROM {schema}.orders
      GROUP BY customer_id
      HAVING COUNT(*) > 1
      """
    Then the query result should have 1 rows
    And the first result column "customer_id" should be "1"

  Scenario: Query with no matching rows returns zero
    Given a managed table "statuses" with data:
      | id | status |
      | 1  | active |
      | 2  | active |
    When I execute a query on the test schema:
      """
      SELECT * FROM {schema}.statuses WHERE status = 'inactive'
      """
    Then the query result should have 0 rows
