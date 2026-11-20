# Reading and Measuring MySQL Execution Plans

## Contents
1. EXPLAIN formats
2. Key plan columns
3. `Extra` values that matter
4. EXPLAIN ANALYZE and bad estimates
5. Finding slow queries
6. Verifying an improvement (before and after)

## 1. EXPLAIN formats

| Command | What it gives | When to use it |
|---|---|---|
| `EXPLAIN SELECT ...` | Classic table, estimates | Quick look |
| `EXPLAIN FORMAT=JSON SELECT ...` | Costs, `used_key_parts`, attached conditions | Analysis with the script |
| `EXPLAIN FORMAT=TREE SELECT ...` (8.0.16+) | Iterator tree | Seeing the real join order |
| `EXPLAIN ANALYZE SELECT ...` (8.0.18+) | Runs the query: actual times and rows | Confirming where the time goes |

`EXPLAIN ANALYZE` **executes** the query. Do not use it on `UPDATE`/`DELETE` in production; convert it to an equivalent `SELECT` first.

## 2. Key columns

**`type`** (access method), from best to worst:
`system` → `const` → `eq_ref` → `ref` → `fulltext` → `ref_or_null` → `index_merge` → `unique_subquery` → `index_subquery` → `range` → `index` → `ALL`

- `ALL`: reads the whole table. Serious on large tables; irrelevant on tables with a few rows.
- `index`: reads the whole index. Better than `ALL` only if the index is narrower than the table (for example, with `Using index`).
- `range`: reads a range of the index (`BETWEEN`, `>`, `IN`, `LIKE 'abc%'`).
- `ref` / `eq_ref`: equality lookup; `eq_ref` is one-to-one in joins on a PK/UNIQUE key.

**`possible_keys` versus `key`**: if there are candidates but `key` is `NULL`, the optimizer rejected them. Typical causes: low selectivity (it estimates that reading the whole table is cheaper), implicit type conversion, mismatched collation in a join, or stale statistics. If the index you expect does not even appear in `possible_keys`, the condition is not indexable as written (function applied to the column, `LIKE '%x'`, etc.).

**`key_len`**: how many bytes of the index are used, and therefore how many columns of a compound index are used. Sizes: `INT` 4, `BIGINT` 8, `DATE` 3, `DATETIME` 5 (+ fractional seconds), `VARCHAR(n)` in utf8mb4 `n*4 + 2`, `CHAR(n)` in utf8mb4 `n*4`; add 1 if the column is nullable. If `key_len` only covers the first column of a three-column index, the rest is not used for filtering (usually because a range condition comes first).

**`rows`** and **`filtered`**: `rows` is the estimated number of rows read per access; `rows × filtered%` is what passes to the next step. A low `filtered` (< 20%) with many `rows` means the condition is evaluated after reading the rows, not inside the index.

**`select_type`**: `DEPENDENT SUBQUERY` means the subquery runs once per outer row; when it appears it is usually the main problem.

## 3. `Extra` values

| Value | Meaning | Interpretation |
|---|---|---|
| `Using index` | Covering index: the table is not read | Good |
| `Using index condition` | Index Condition Pushdown: filters inside the index | Good |
| `Using where` | Filters after reading | Normal; serious with `type: ALL` and many rows |
| `Using filesort` | Sorts separately, without an index | Expensive with many rows |
| `Using temporary` | Temporary table (GROUP BY, DISTINCT, UNION) | Expensive with many rows |
| `Using join buffer (hash join)` / `(Block Nested Loop)` | No index on the JOIN column | Almost always a missing index |
| `Using MRR` | Reads in PK order after the index | Neutral |
| `Impossible WHERE` | The condition can never be true | Check the logic |
| `Range checked for each record` | No good index found; re-evaluated per row | A suitable index is missing |

In `FORMAT=JSON` the equivalents are `using_filesort`, `using_temporary_table`, `using_join_buffer`, `using_index`, and the condition appears in `attached_condition`.

## 4. EXPLAIN ANALYZE and bad estimates

Each line shows the estimate and the actual values:

```
-> Filter: (o.status = 'paid')  (cost=50123 rows=49800) (actual time=0.08..310 rows=124500 loops=1)
```

- `actual time=a..b`: milliseconds to the first row and to the last row, **per iteration**.
- Total actual rows = `rows × loops`.
- If the estimate and reality differ by more than 10×, the optimizer is deciding with wrong data. Fix: `ANALYZE TABLE t;` and, for non-indexed columns with skewed distributions, histograms: `ANALYZE TABLE t UPDATE HISTOGRAM ON col WITH 64 BUCKETS;` (8.0+).
- `Table scan on t` = full scan; `Index scan on t using idx` = full index scan; `Inner hash join` = join without a usable index.

## 5. Finding slow queries

```sql
-- Slow query log
SET GLOBAL slow_query_log = 'ON';
SET GLOBAL long_query_time = 0.5;               -- seconds
SET GLOBAL log_queries_not_using_indexes = 'ON'; -- optional, noisy

-- performance_schema / sys (8.0, enabled by default)
SELECT query, exec_count, avg_latency, rows_examined_avg, rows_sent_avg
FROM sys.statement_analysis ORDER BY avg_latency DESC LIMIT 10;

SELECT * FROM sys.statements_with_full_table_scans LIMIT 10;
SELECT * FROM sys.schema_unused_indexes;     -- candidates for removal
SELECT * FROM sys.schema_redundant_indexes;  -- indexes covered by others
```

`rows_examined_avg / rows_sent_avg` is the best quick indicator: above 100 there is almost always something to improve. To summarize the slow log: `pt-query-digest slow.log` (Percona Toolkit).

## 6. Verifying an improvement

**Handler counters**: a deterministic measure with no cache noise.

```sql
FLUSH STATUS;
SELECT ...;            -- the query
SHOW SESSION STATUS LIKE 'Handler_read%';
```

High `Handler_read_rnd_next` = full scan. `Handler_read_next` = rows read through an index. After optimizing, the total should approach the number of rows returned.

**Testing an index without affecting other sessions** (MySQL 8.0+): create it invisible and enable it only in your session.

```sql
CREATE INDEX idx_status_created ON orders (status, created_at) INVISIBLE;
SET SESSION optimizer_switch = 'use_invisible_indexes=on';
EXPLAIN ANALYZE SELECT ...;     -- plan with the new index
SET SESSION optimizer_switch = 'use_invisible_indexes=off';
-- If it helps:
ALTER TABLE orders ALTER INDEX idx_status_created VISIBLE;
-- Rollback:
DROP INDEX idx_status_created ON orders;
```

An invisible index is still maintained on every write, so the write cost exists from the moment it is created. To drop an index safely, make it invisible first, observe, and then drop it.

**Timing**: run the query 3–5 times and use the median; the first run usually pays for loading the buffer pool. MySQL 8.0 has no query cache, so `SQL_NO_CACHE` no longer applies.

**Building in production**: `ALTER TABLE t ADD INDEX ..., ALGORITHM=INPLACE, LOCK=NONE;` allows concurrent writes in InnoDB but consumes I/O; for very large tables consider `pt-online-schema-change` or `gh-ost`.
