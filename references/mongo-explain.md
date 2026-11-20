# Reading and Measuring MongoDB Execution Plans

## Contents
1. Explain modes and getting JSON
2. Key metrics
3. Plan stages
4. SBE engine (7.0+) and aggregations
5. Finding slow queries
6. Verifying an improvement

## 1. Explain modes

| Mode | What it gives |
|---|---|
| `"queryPlanner"` (default) | Winning and rejected plans; does not execute |
| `"executionStats"` | Executes the winning plan: actual documents, keys and time |
| `"allPlansExecution"` | Also partial statistics for the rejected plans |

Always use `"executionStats"` to diagnose. To get valid JSON (mongosh prints JavaScript syntax, not JSON):

```javascript
EJSON.stringify(db.orders.find({status: "paid"}).sort({createdAt: -1}).explain("executionStats"))
EJSON.stringify(db.orders.explain("executionStats").aggregate([ ... ]))
```

## 2. Key metrics

In `executionStats`:

| Metric | Meaning |
|---|---|
| `nReturned` | Documents returned |
| `totalKeysExamined` | Index entries read |
| `totalDocsExamined` | Documents read from the collection |
| `executionTimeMillis` | Total time |

**Ideal ratio**: `nReturned ≈ totalKeysExamined ≈ totalDocsExamined`.

- `totalDocsExamined` ≫ `nReturned`: the index does not filter enough, or there is no index. Above 10:1 it deserves a look; above 100:1 it is almost certainly the main problem.
- `totalKeysExamined` ≫ `nReturned`: the index is used but scans too wide a range (wrong field order, range before equality).
- `totalDocsExamined = 0` with `nReturned > 0`: **covered query**, the result comes from the index alone. Optimal.

## 3. Plan stages

The plan is a tree (`inputStage` / `inputStages`); read it from the leaves up to the root.

| Stage | Meaning | Interpretation |
|---|---|---|
| `COLLSCAN` | Scans the entire collection | Serious on large collections |
| `IXSCAN` | Scans an index | Good; check `indexBounds` |
| `FETCH` | Reads the document after the index | Normal; if it has a `filter`, some predicates are not covered by the index |
| `SORT` | Sorts in memory | Expensive; 100 MB limit by default, may spill to disk (`usedDisk: true`) |
| `PROJECTION_COVERED` | Projection from the index | Covered query |
| `SKIP` | Discards documents | Expensive with large values |
| `OR` / `SUBPLAN` | Each `$or` branch with its own plan | Each branch needs its own index |
| `IDHACK` | Direct lookup by `_id` | Optimal |
| `COUNT_SCAN` / `DISTINCT_SCAN` | Count or distinct from the index | Good |
| `SHARD_MERGE` / `SINGLE_SHARD` | Results from shards | Check whether the query hits every shard |

**`indexBounds`** in `IXSCAN`: shows the range scanned for each index field. `[MinKey, MaxKey]` on the first field means the whole index is scanned (it is only being used for sorting, or the filter does not use it). Bounded ranges on all equality fields are a good sign.

**`isMultiKey: true`**: the index includes an array field. Range bounds may be looser and the index cannot cover the query.

**`rejectedPlans`**: candidate plans that were discarded. Many candidates indicate overlapping indexes; the chosen plan is stored in the *plan cache* and reused for queries with the same shape.

## 4. SBE engine (7.0+) and aggregations

With the slot-based execution engine (SBE), `queryPlanner.winningPlan` contains `queryPlan` (a tree with the same stages as above) and `slotBasedPlan` (internal detail, can be ignored), and `explainVersion` is `"2"`. Parts of the pipeline (`$group`, `$lookup`) run inside the query engine and appear as `GROUP` or `EQ_LOOKUP` stages.

`EQ_LOOKUP` includes a `strategy`:

- `IndexedLoopJoin`: uses an index on `foreignField`. Good.
- `HashJoin`: loads the foreign collection into memory (only if it is small). Acceptable.
- `NestedLoopJoin`: scans the foreign collection **for every input document**. An index on `foreignField` is missing.

In the classic engine, the explain output of an `aggregate` has a `stages` list: the first one (`$cursor`) contains the plan for the part of the filter that uses indexes, and the following stages show `nReturned` and `executionTimeMillisEstimate`. A `$lookup` stage reports `totalDocsExamined`, `collectionScans` and `indexesUsed`: `collectionScans > 0` means an index on `foreignField` is missing.

Only the leading `$match` and `$sort` stages of a pipeline can use indexes. The optimizer reorders some stages automatically (for example, it moves a `$match` before a `$project` or `$unwind` if it does not depend on them), so always compare the pipeline as written with what `explain` shows.

## 5. Finding slow queries

```javascript
// Profiler: level 1 records operations slower than slowms
db.setProfilingLevel(1, { slowms: 100 })
db.system.profile.find({ millis: { $gt: 100 } })
  .sort({ ts: -1 }).limit(10)
  .projection({ ns: 1, millis: 1, planSummary: 1, keysExamined: 1, docsExamined: 1, nreturned: 1, command: 1 })

// Operations running for more than 5 s
db.currentOp({ secs_running: { $gt: 5 } })

// Index usage since the last restart
db.orders.aggregate([{ $indexStats: {} }])
```

`planSummary: "COLLSCAN"` in the profiler is the fastest signal. The profiler has a cost: in production use a reasonable `slowms` and turn it off afterwards (`db.setProfilingLevel(0)`). In Atlas, the Query Profiler and the Performance Advisor provide this information without enabling it manually.

## 6. Verifying an improvement

**Compare plans with `hint()`** (forces an existing index):

```javascript
db.orders.find(q).sort(s).hint({ status: 1, createdAt: -1 }).explain("executionStats")
db.orders.find(q).sort(s).hint({ $natural: 1 }).explain("executionStats")  // forces COLLSCAN
```

Compare `totalDocsExamined`, `totalKeysExamined`, the presence of `SORT` and `executionTimeMillis` before and after.

**Testing a new index**: unlike MySQL, MongoDB cannot enable an index for a single session. Hidden indexes (`hideIndex`, 4.4+) serve the opposite purpose: stop using an index without dropping it, check that nothing gets worse, and then drop it. To test a new index, create it in a staging environment or on a copy of the collection with representative data, or create it off-peak with the rollback ready:

```javascript
db.orders.createIndex({ status: 1, createdAt: -1, total: 1 }, { name: "idx_status_createdAt_total" })
// Rollback
db.orders.dropIndex("idx_status_createdAt_total")
// Retiring an index safely
db.orders.hideIndex("status_1")   // observe
db.orders.dropIndex("status_1")   // if nothing gets worse
```

Building an index on a large collection consumes CPU and I/O and, in replica sets, is replicated to the secondaries. Plan for it.

**Plan cache**: after creating indexes, the cached plan may keep being used until it is re-evaluated. For testing: `db.orders.getPlanCache().clear()`.
