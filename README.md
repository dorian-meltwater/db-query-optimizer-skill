# db-query-optimizer

A [Claude Code skill](https://docs.claude.com/en/docs/claude-code/skills) that diagnoses and optimizes slow **MySQL** and **MongoDB** queries from the query, the schema and the execution plan.

Instead of guessing, the skill works like a senior DBA: it reads the plan (`EXPLAIN`, `EXPLAIN ANALYZE`, `explain("executionStats")`), finds the root cause, proposes rewrites and indexes, and gives you the commands to prove the improvement.

## What it does

- Identifies the engine and version (MySQL 8.0 / MongoDB 7.0 assumed if not stated).
- Gathers the query, plan, schema, indexes and data volume. If the plan is missing, findings are labelled as hypotheses and the exact command to get the plan is provided.
- Runs a deterministic analyzer over the plan to catch signals that are easy to miss by eye.
- Ranks problems by impact and separates what is **confirmed by the plan** from what is **inferred**.
- Proposes fixes in order of preference: rewrite the query, adjust an existing index, create a new index, change the data model.
- Always includes before/after verification commands and a rollback for any DDL.

It triggers automatically when you paste a SQL query, `find()` or aggregation pipeline and mention slowness, indexes, `COLLSCAN`, filesort, execution plans, etc. It also works for requests in Spanish or other languages.

## Project layout

```
.
├── SKILL.md                         # Skill definition: workflow, safety rules, report format
├── references/
│   ├── mysql-explain.md             # How to read MySQL plans and measure improvements
│   ├── mysql-antipatterns.md        # 15 MySQL antipatterns with plan signals and fixes
│   ├── mongo-explain.md             # How to read MongoDB plans (classic and SBE)
│   └── mongo-antipatterns.md        # 15 MongoDB antipatterns with plan signals and fixes
└── scripts/
    ├── analyze_mysql_explain.py     # MySQL plan analyzer
    ├── analyze_mongo_explain.py     # MongoDB plan analyzer
    └── examples/                    # Sample plans for testing both analyzers
```

## Installation

Copy or clone this folder into your skills directory:

```bash
# Personal (all projects)
git clone <repo-url> ~/.claude/skills/db-query-optimizer

# Or per project
git clone <repo-url> .claude/skills/db-query-optimizer
```

Claude Code picks it up automatically; no configuration is needed.

## Using the analyzer scripts

Both scripts use only the Python 3 standard library and can also be used on their own.

### MySQL

```bash
python scripts/analyze_mysql_explain.py plan.txt
cat plan.txt | python scripts/analyze_mysql_explain.py -
python scripts/analyze_mysql_explain.py plan.txt --json
```

Accepted formats (auto-detected): `EXPLAIN FORMAT=JSON`, tabular `EXPLAIN`, vertical `\G` output, and `EXPLAIN ANALYZE` / `FORMAT=TREE`.

Get a plan:

```sql
EXPLAIN FORMAT=JSON SELECT ...;
EXPLAIN ANALYZE SELECT ...;   -- MySQL 8.0.18+, runs the query
```

### MongoDB

```bash
python scripts/analyze_mongo_explain.py plan.json
cat plan.json | python scripts/analyze_mongo_explain.py -
python scripts/analyze_mongo_explain.py plan.json --json
```

Accepts `find()` and `aggregate()` plans, classic and SBE (7.0+) engines, sharded plans, and raw mongosh output.

Get a plan (in `mongosh`):

```javascript
EJSON.stringify(db.orders.find({...}).sort({...}).explain("executionStats"))
EJSON.stringify(db.orders.explain("executionStats").aggregate([...]))
```

### Example

```console
$ python scripts/analyze_mysql_explain.py scripts/examples/mysql_json_year.json
Detected format: json
approx_rows_examined: 498120

Findings:
  [HIGH] INDEX_IGNORED (orders): Full table scan (~498120 rows) even though candidate indexes exist: idx_status.
  [HIGH] FILESORT (orders): Sort without an index (Using filesort).
  [HIGH] FUNCTION_ON_COLUMN (orders): The condition applies a function to a column: ((year(`shop`.`orders`.`created_at`) = 2025) ...
  [MEDIUM] LOW_FILTERED (orders): filtered=2.5%: ~498120 rows are read and only ~12453 pass the filter.
```

```console
$ python scripts/analyze_mongo_explain.py scripts/examples/mongo_find_collscan.json
Findings:
  [HIGH] HIGH_DOCS_RATIO: Examines 300000 documents to return 6 (ratio 50000:1).
  [HIGH] COLLSCAN: Full collection scan (300000 documents).
  [MEDIUM] IN_MEMORY_SORT: In-memory sort.
```

The findings are signals, not the final diagnosis: interpret them together with the query and the schema.

## Sample plans

`scripts/examples/` contains ready-to-run inputs:

| File | Scenario |
| --- | --- |
| `mysql_json_year.json` | `YEAR()` on an indexed column (FORMAT=JSON) |
| `mysql_tabular_join.txt` | Join, tabular output |
| `mysql_vertical_phone.txt` | Vertical (`\G`) output |
| `mysql_analyze_offset.txt` | `EXPLAIN ANALYZE` with large `OFFSET` |
| `mongo_find_collscan.json` | `COLLSCAN` plus in-memory sort |
| `mongo_find_esr_mongosh.txt` | Raw mongosh output, ESR index issue |
| `mongo_covered.json` | Covered query (healthy plan) |
| `mongo_agg_lookup_classic.json` | `$lookup`, classic engine |
| `mongo_agg_lookup_sbe.json` | `$lookup`, SBE engine |

## Safety

- DDL is never recommended for direct production use without review; the impact (locking, build cost) and the rollback (`DROP INDEX` / `dropIndex`) are always stated.
- No statistics, timings or cardinalities are invented.
- If the plan contradicts a hypothesis, the plan wins.
- You never need to share real data to optimize a query. Anonymize anything containing personal information.

## Report format

Every response follows the same structure: **Summary → Diagnosis** (ranked HIGH/MEDIUM/LOW with evidence and cause) **→ Proposal** (optimized query, indexes, expected impact) **→ How to verify → Risks and considerations**. See [SKILL.md](SKILL.md) for details.
