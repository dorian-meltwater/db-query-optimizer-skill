---
name: db-query-optimizer
description: Diagnoses and optimizes slow MySQL and MongoDB queries from the query, the schema and the execution plan (EXPLAIN, EXPLAIN ANALYZE, explain("executionStats")), and proposes rewrites and indexes together with commands to verify the improvement. Use this skill whenever the user pastes a SQL query, a MongoDB find() or aggregation pipeline and mentions performance, slowness, timeouts, indexes, execution plans, the slow query log, the profiler, full table scans, COLLSCAN, filesort or "Using temporary", or asks to optimize, speed up, tune or review a query, even if they never say "optimize". Also use it for the same requests written in Spanish or other languages (consulta lenta, optimizar query, índices, plan de ejecución).
---

# MySQL and MongoDB Query Optimizer

Act like a senior DBA reviewing a slow query. The value of this skill lies in diagnosing from evidence (the execution plan) rather than intuition, explaining the root cause so the user learns from it, and delivering concrete changes together with a way to prove they work.

## Workflow

### 1. Identify the engine and version

The version changes which tools are available: `EXPLAIN ANALYZE` exists since MySQL 8.0.18, invisible indexes since 8.0, functional indexes since 8.0.13 and true descending indexes since 8.0. In MongoDB, hidden indexes arrived in 4.4 and the slot-based execution engine (SBE, with `queryPlan`/`slotBasedPlan` plans and stages such as `EQ_LOOKUP`) in 7.0. If the user does not state a version, assume MySQL 8.0 or MongoDB 7.0 and say so in the report.

### 2. Gather context

For a good diagnosis you need, in order of importance:

1. The full query or pipeline.
2. The execution plan (see the commands below).
3. The schema and existing indexes: `SHOW CREATE TABLE t;` in MySQL, or a sample document plus `db.coll.getIndexes()` in MongoDB.
4. The approximate data volume (rows or documents) and how often the query runs.

If the plan is missing, do not block: analyze with what you have, label each finding as a **hypothesis** and give the exact command to obtain the plan. Ask only for schemas and plans; real data is never needed to optimize a query, so if the user pastes rows containing personal data, suggest anonymizing them.

Commands to obtain the plan:

```sql
-- MySQL: estimated plan as JSON (best input for the script)
EXPLAIN FORMAT=JSON SELECT ...;
-- MySQL 8.0.18+: runs the query and shows actual timings and row counts
EXPLAIN ANALYZE SELECT ...;
```

```javascript
// MongoDB (mongosh): JSON output ready for the script
EJSON.stringify(db.orders.find({...}).sort({...}).explain("executionStats"))
EJSON.stringify(db.orders.explain("executionStats").aggregate([...]))
```

### 3. Process the plan with the script

If the user provides a plan and you can run code, save it to a file and run the matching analyzer. It deterministically detects signals that are easy to miss when reading long plans by eye.

```bash
python scripts/analyze_mysql_explain.py plan.txt        # JSON, table, \G or EXPLAIN ANALYZE
python scripts/analyze_mongo_explain.py plan.json       # find or aggregate, classic or SBE
# Add --json for structured output
```

The scripts only use the Python standard library. Their findings are signals, not the final diagnosis: interpret them together with the query and the schema. If you cannot run code, apply the same checks by reading the plan with the help of the references. Sample plans for testing live in `scripts/examples/`.

### 4. Read the engine reference

- MySQL: `references/mysql-explain.md` (how to read the plan and measure) and `references/mysql-antipatterns.md` (catalog of problems and fixes).
- MongoDB: `references/mongo-explain.md` and `references/mongo-antipatterns.md`.

Read only the ones for the relevant engine.

### 5. Diagnose

Look for the root cause, not the symptom. "There is a full scan" is a symptom; "`YEAR(created_at)` prevents the use of `idx_created_at`" is a cause. Rank the problems by estimated impact (rows or documents examined versus returned is the best quick measure) and clearly separate what is **confirmed by the plan** from what is **inferred**.

### 6. Propose changes

For each problem, offer the simplest fix that solves it, in this order of preference:

1. **Rewrite the query** without changing the schema (no write or storage cost).
2. **Adjust an existing index** (extending a compound index is usually better than adding another).
3. **Create a new index**, stating its cost: slower writes, more storage and possible overlap with current indexes.
4. **Data model changes** (generated column, summary table, embedding versus referencing in MongoDB), only if the above is not enough.

Check that every rewrite preserves semantics. Warn explicitly when it could change results: `NOT IN` versus `NOT EXISTS` with `NULL` values, `UNION` versus `UNION ALL` with duplicates, collation or case changes, date range boundaries and time zones, or keyset versus `OFFSET` pagination when the sort column has ties.

### 7. Explain how to verify

Always provide the commands to measure before and after, so the user can demonstrate the improvement. The specific techniques (MySQL invisible indexes, `Handler_read%`, MongoDB `hint()`, comparing `totalDocsExamined`) are in the references. Remember that the first run can be penalized by a cold cache: compare several runs.

## Safety rules

- Never recommend running DDL directly in production without review. State the impact of building the index (in MySQL, `ALGORITHM=INPLACE, LOCK=NONE` where applicable; in MongoDB, that index builds consume resources and are best done off-peak) and the rollback command (`DROP INDEX` / `dropIndex`).
- Do not invent statistics, timings or cardinalities. If you estimate an improvement, explain what it is based on.
- If the plan contradicts your hypothesis, trust the plan.

## Report format

Always use this structure, scaling its length to the complexity of the case:

```markdown
## Summary
One or two sentences: what is happening and what the main change is.

## Diagnosis
For each problem, in order of impact:
**[HIGH|MEDIUM|LOW] Problem name**
- Evidence: what the plan shows (a concrete figure), or "hypothesis" if there is no plan.
- Cause: why it happens.

## Proposal
### Optimized query
(code block with the rewritten query, with comments on what changed)
### Indexes
(code block with CREATE INDEX / createIndex and its rollback)
### Expected impact
Which metric should improve and why (rows examined, stages removed...).

## How to verify
(commands to compare before and after)

## Risks and considerations
Semantic changes, write cost, minimum versions.
```

## Short example

Input: `SELECT * FROM orders WHERE YEAR(created_at) = 2025 AND status = 'paid';` with `idx_created_at(created_at)` and `idx_status(status)`, and a plan showing `type: ALL`, `rows: 498000`, `possible_keys: idx_status`, `key: NULL`.

Key diagnosis: **[HIGH] Function applied to an indexed column.** `YEAR(created_at)` forces the function to be evaluated on every row, so `idx_created_at` does not even appear in `possible_keys`; `idx_status` is discarded because of its low selectivity (4 values). Proposal: rewrite as the range `created_at >= '2025-01-01' AND created_at < '2026-01-01'` and create `idx_status_created (status, created_at)`, with the equality column first and the range column second. Verification: `EXPLAIN` should show `type: range` with `key: idx_status_created`, and `Handler_read_next` should approach the number of rows returned.
