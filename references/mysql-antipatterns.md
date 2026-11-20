# MySQL Performance Antipatterns

Each entry lists the plan signal that reveals it and the fix. They are ordered roughly by frequency.

## Contents
1. Function applied to an indexed column
2. Implicit type or collation conversion
3. `LIKE` with a leading wildcard
4. Badly ordered compound index
5. `OR` across different columns
6. `SELECT *` and lack of coverage
7. Pagination with a large `OFFSET`
8. Dependent subqueries and `NOT IN`
9. JOIN without an index on the join column
10. `GROUP BY` / `ORDER BY` without an index
11. `COUNT(*)` on huge tables
12. N+1 pattern from the ORM
13. Stale statistics
14. Redundant or unused indexes
15. `ORDER BY RAND()`

---

## 1. Function applied to an indexed column

```sql
WHERE YEAR(created_at) = 2025
WHERE DATE(created_at) = '2025-03-01'
WHERE LOWER(email) = 'a@b.com'
WHERE price * 1.16 > 100
```

**Signal**: `type: ALL` and the column's index does not appear in `possible_keys`; in JSON, `attached_condition` contains the function.
**Why**: the index is sorted by the column value, not by the function result, so it cannot be searched.
**Fix**: isolate the column.

```sql
WHERE created_at >= '2025-01-01' AND created_at < '2026-01-01'
WHERE price > 100 / 1.16
```

If it cannot be rewritten (for example, `LOWER(email)` because the data has mixed case), use a functional index (8.0.13+): `CREATE INDEX idx_email_lower ON users ((LOWER(email)));`, or an indexed generated column. With `_ci` collations (the usual ones) the comparison is already case-insensitive and `LOWER()` is unnecessary.

## 2. Implicit type or collation conversion

```sql
-- phone is VARCHAR, compared with a number
WHERE phone = 5512345678
-- join between utf8mb3 and utf8mb4, or between INT and VARCHAR
```

**Signal**: `possible_keys` includes the index but `key: NULL` and `type: ALL`; `SHOW WARNINGS` after `EXPLAIN` shows the conversion warning.
**Why**: MySQL converts each column value to compare it, which is equivalent to applying a function.
**Fix**: use a literal of the column's type (`WHERE phone = '5512345678'`) and align types, charset and collation on columns joined together.

## 3. `LIKE` with a leading wildcard

```sql
WHERE email LIKE '%@gmail.com'
WHERE name LIKE '%garcia%'
```

**Signal**: `type: ALL` or `type: index`.
**Fix**: `LIKE 'abc%'` does use an index. To search suffixes, store the reversed column (`REVERSE(email)`) in an indexed generated column and search by prefix. For free text, `FULLTEXT` with `MATCH ... AGAINST` (with the `ngram` parser for short substrings) or an external search engine.

## 4. Badly ordered compound index

Rules for compound indexes:

- **Leftmost prefix**: `(a, b, c)` serves `a`, `a,b` and `a,b,c`; not `b` or `c` alone.
- **Equality first, range after**: columns after the first range condition do not narrow the search (ICP can filter with them, but the entries are still read).
- **Ordering**: `ORDER BY` uses the index if the preceding columns have equality conditions and the sort columns follow in the same sequence and direction. For mixed directions (`ORDER BY a ASC, b DESC`) use descending indexes (8.0+): `(a ASC, b DESC)`.
- **Selectivity**: among equality columns the order matters less, but it helps to start with the one reused by other queries.

**Signal**: `key_len` covers fewer columns than expected, `Using filesort` despite an existing index, or low `filtered`.

```sql
-- Query: WHERE status = 'paid' AND created_at >= ? ORDER BY created_at
-- Bad:  (created_at, status)   -> range first, status does not narrow the search
-- Good: (status, created_at)   -> equality, then range and sort in the same index
```

## 5. `OR` across different columns

```sql
WHERE customer_id = 10 OR email = 'x@y.com'
```

**Signal**: `type: ALL`, or `index_merge` with `Using union(...)` (sometimes acceptable).
**Fix**: `UNION ALL` with one branch per index; use `UNION` (which removes duplicates) if a row can match both branches, or exclude the intersection in the second branch.

```sql
SELECT ... WHERE customer_id = 10
UNION
SELECT ... WHERE email = 'x@y.com';
```

`OR` on the **same** column is equivalent to `IN (...)` and is not a problem.

## 6. `SELECT *` and lack of coverage

**Signal**: `type: ref`/`range` without `Using index`, with many rows: each index entry triggers a table lookup by PK.
**Fix**: select only the columns you need; if the query is very frequent, create a covering index that includes the `SELECT` columns (in InnoDB the PK is implicitly included in every secondary index). Do not turn indexes into copies of the table: coverage only for small columns and hot queries.

## 7. Pagination with a large `OFFSET`

```sql
SELECT * FROM orders ORDER BY created_at DESC LIMIT 20 OFFSET 400000;
```

**Signal**: huge `rows` even though 20 rows are returned; time grows with the page number.
**Why**: MySQL reads and discards the previous 400,000 rows.
**Fix**: keyset pagination, remembering the last value seen. Include a unique column as a tie-breaker.

```sql
SELECT * FROM orders
WHERE (created_at, id) < ('2025-06-01 10:00:00', 812345)
ORDER BY created_at DESC, id DESC
LIMIT 20;
-- Index: (created_at, id)
```

If jumping to an arbitrary page is required, use a *deferred join*: paginate over the index and then fetch the rows.

```sql
SELECT o.* FROM orders o
JOIN (SELECT id FROM orders ORDER BY created_at DESC LIMIT 20 OFFSET 400000) p USING (id)
ORDER BY o.created_at DESC;
```

Semantics: keyset changes the user experience (there is no "page 873") and requires a total order; mention it.

## 8. Dependent subqueries and `NOT IN`

**Signal**: `select_type: DEPENDENT SUBQUERY`.
**Fix**: rewrite as a JOIN, `EXISTS` or derived table. In 8.0 the optimizer automatically converts many `IN (subquery)` into semi-joins; the rewrite matters mostly on 5.7 and for subqueries in the `SELECT` list.

`NOT IN (subquery)` returns **zero rows** if the subquery contains any `NULL`. `NOT EXISTS` or `LEFT JOIN ... WHERE x IS NULL` do not have that problem, which means the rewrite can change the result: say so.

```sql
SELECT c.id FROM customers c
WHERE NOT EXISTS (SELECT 1 FROM orders o WHERE o.customer_id = c.id AND o.status = 'cancelled');
-- Index: orders (customer_id, status)
```

## 9. JOIN without an index on the join column

**Signal**: `Using join buffer (hash join)` or `(Block Nested Loop)`; in `EXPLAIN ANALYZE`, `Inner hash join`.
**Fix**: index the join column of the inner table, usually the FK (`orders.customer_id`). InnoDB creates that index automatically only if the `FOREIGN KEY` is declared. If there is also a filter, a compound `(customer_id, status)` covers both.

## 10. `GROUP BY` / `ORDER BY` without an index

**Signal**: `Using temporary; Using filesort`.
**Fix**: an index whose columns start with the `GROUP BY` columns (after the `WHERE` equality columns) allows grouping in order without a temporary table. If the sort is on an aggregate (`ORDER BY SUM(total)`), the filesort is unavoidable: reduce the rows before grouping, or maintain a summary table if the query is very frequent.

## 11. `COUNT(*)` on huge tables

InnoDB does not store the exact row count; `COUNT(*)` scans the smallest index. If an approximation is enough, use `information_schema.TABLES.TABLE_ROWS` or `EXPLAIN`. If exact counts are frequent, keep a counter in a summary table.

## 12. N+1 pattern from the ORM

It is not visible in a single plan but in the slow log or `sys.statement_analysis`: the same query with a very high `exec_count` and low individual latency. Fix: batch loading (`WHERE id IN (...)`), a JOIN, or the ORM's *eager loading*.

## 13. Stale statistics

**Signal**: in `EXPLAIN ANALYZE`, estimate and reality differ by more than 10×; the optimizer intermittently picks a bad index.
**Fix**: `ANALYZE TABLE t;` and histograms for skewed columns. As a last resort, `FORCE INDEX` or optimizer hints (8.0: `/*+ INDEX(t idx) */`), knowing that they pin the plan even when the data changes.

## 14. Redundant or unused indexes

`(a)` is redundant if `(a, b)` exists. Every index slows down `INSERT`/`UPDATE`/`DELETE` and takes buffer pool space. Check `sys.schema_redundant_indexes` and `sys.schema_unused_indexes` (the latter is only reliable after a representative traffic period since the last restart). Make them invisible before dropping them.

## 15. `ORDER BY RAND()`

It generates a random value per row and sorts the whole table. Alternatives: pick a random id within the range and take the next existing one (`WHERE id >= ? ORDER BY id LIMIT 1`), or precompute an indexed random column.
