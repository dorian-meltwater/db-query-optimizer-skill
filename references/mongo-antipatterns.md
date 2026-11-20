# MongoDB Performance Antipatterns

Each entry lists the plan signal and the fix.

## Contents
1. Missing index (COLLSCAN)
2. Compound index that does not follow the ESR rule
3. In-memory sort
4. Unanchored or case-insensitive regular expressions
5. Negation operators
6. `$or` without an index on every branch
7. Pagination with a large `skip()`
8. Badly ordered aggregation pipeline
9. `$lookup` without an index on `foreignField`
10. `$expr` and functions applied to fields
11. Fetching whole documents
12. Unbounded arrays and multikey indexes
13. Counts on large collections
14. Too many or unused indexes
15. Data model that does not match the read pattern

---

## 1. Missing index (COLLSCAN)

**Signal**: `COLLSCAN` stage, `totalDocsExamined` ≈ collection size.
**Fix**: an index on the filter fields following the ESR rule (next item). On collections with a few hundred documents, a `COLLSCAN` is not a problem.

## 2. Compound index that does not follow the ESR rule

Recommended field order for a compound index:

1. **E**quality: fields compared by equality.
2. **S**ort: fields in the `sort()`, in the same order.
3. **R**ange: fields with `$gt`, `$lt`, wide `$in`, `$ne`, regex.

```javascript
// Query
db.orders.find({ status: "paid", total: { $gt: 500 } }).sort({ createdAt: -1 }).limit(20)

// Bad:  { status: 1, total: 1, createdAt: -1 }  -> the range before the sort forces an in-memory sort
// Good: { status: 1, createdAt: -1, total: 1 }  -> reads in order and filters total inside the index
```

**Signal**: `SORT` in the plan even though there is an index; `totalKeysExamined` ≫ `nReturned`; `indexBounds` with `[MinKey, MaxKey]` on fields that should be bounded.

The index can serve the sort if the directions match those of the `sort()` or are all inverted: `{a: 1, b: -1}` serves `sort({a: 1, b: -1})` and `sort({a: -1, b: 1})`, but not `sort({a: 1, b: 1})`.

`$in` with a few values behaves like equality for filtering but can break the sort order: with `$in` followed by `sort`, MongoDB has to merge several ranges (`SORT_MERGE` stage) or sort in memory.

## 3. In-memory sort

**Signal**: `SORT` stage; if `usedDisk: true`, it exceeded the memory limit (100 MB by default) and spilled to disk.
**Fix**: an index that returns the documents already sorted (see ESR). Combining `sort` with `limit` reduces the cost (*top-k sort*), but it still examines every document that matches the filter.

## 4. Unanchored or case-insensitive regular expressions

```javascript
{ email: /garcia/ }     // unanchored: scans the whole index or collection
{ email: /^garcia/i }   // /i prevents tight bounds even when anchored
```

**Signal**: `IXSCAN` with wide `indexBounds` and `totalKeysExamined` ≈ index size, or `COLLSCAN`.
**Fix**: an anchored, case-sensitive regex (`/^garcia/`) uses efficient ranges. For case-insensitive search, create an index with a strength-2 collation and query with the same collation, or store a normalized lowercase field. For free text, a `text` index or Atlas Search.

```javascript
db.customers.createIndex({ email: 1 }, { collation: { locale: "en", strength: 2 } })
db.customers.find({ email: "Ana@Example.com" }).collation({ locale: "en", strength: 2 })
```

## 5. Negation operators

`$ne`, `$nin` and `$not` are almost never selective: they return most documents and the index barely helps. If the possible values are few and known, rewrite as `$in` with the desired values (`status: { $in: ["paid", "shipped"] }` instead of `status: { $ne: "cancelled" }`).

## 6. `$or` without an index on every branch

**Signal**: `COLLSCAN` with an `$or` in the filter, even though some branches have indexes.
**Why**: each `$or` clause is planned separately; if a single branch has no index, the whole collection is scanned.
**Fix**: an index for each branch. An `$or` on the same field is equivalent to `$in` and does not have this problem.

## 7. Pagination with a large `skip()`

**Signal**: `SKIP` stage; `totalKeysExamined` ≈ skip + limit.
**Fix**: range-based pagination, remembering the last value seen. Include `_id` as a tie-breaker.

```javascript
db.orders.find({ createdAt: { $lt: lastCreatedAt } }).sort({ createdAt: -1 }).limit(20)
// With possible ties on createdAt:
db.orders.find({ $or: [
  { createdAt: { $lt: lastCreatedAt } },
  { createdAt: lastCreatedAt, _id: { $lt: lastId } }
]}).sort({ createdAt: -1, _id: -1 }).limit(20)
// Index: { createdAt: -1, _id: -1 }
```

## 8. Badly ordered aggregation pipeline

Only a `$match` and a `$sort` at the **start** of the pipeline can use indexes. Rules:

- `$match` as early as possible, on indexed fields.
- `$sort` + `$limit` together and early if possible (the optimizer merges them into a top-k).
- `$unwind` and `$lookup` after filtering and limiting.
- `$project` / `$addFields` that rename or compute fields before a `$match` prevent that `$match` from using indexes.
- `$group` keeps every group in memory (100 MB limit per stage unless `allowDiskUse`); `$group` over millions of distinct keys is expensive.

The optimizer moves some stages on its own (for example, a `$match` that does not depend on a preceding `$unwind`); check the actual pipeline in `explain` before proposing a reorder.

## 9. `$lookup` without an index on `foreignField`

**Signal**: in the classic engine, the `$lookup` stage shows `collectionScans > 0`; in SBE, `EQ_LOOKUP` with `strategy: "NestedLoopJoin"`.
**Why**: for every input document the whole foreign collection is scanned. With 1,000 input documents and 300,000 in the foreign collection, that is 300 million documents examined.
**Fix**: an index on `foreignField` in the foreign collection; filter and limit before the `$lookup`. If the `$lookup` is the application's main query, consider embedding or duplicating the needed data (item 15).

## 10. `$expr` and functions applied to fields

```javascript
{ $expr: { $eq: [{ $year: "$createdAt" }, 2025] } }
```

**Signal**: `COLLSCAN`, or an unbounded `IXSCAN` with a `FETCH` that has a `filter`.
**Fix**: express the condition as a range on the original field. `$expr` can use indexes only for simple comparisons between a field and a constant; any operator applied to the field prevents it.

```javascript
{ createdAt: { $gte: ISODate("2025-01-01"), $lt: ISODate("2026-01-01") } }
```

Watch out for time zones: `$year` uses UTC by default; adjust the range boundaries if the business works in another zone.

## 11. Fetching whole documents

Use a projection to return only the fields you need: it reduces network and memory usage. For a **covered** query (no `FETCH`), every field in the filter and the projection must be in the index, and `_id` must be explicitly excluded (`{ _id: 0 }`) unless it is part of the index. A multikey index cannot cover queries.

## 12. Unbounded arrays and multikey indexes

Arrays that grow without limit (logs, comments, events inside the document) enlarge every document, make every read and write more expensive, and approach the 16 MB limit. An index on an array creates one entry per element. A compound index cannot include two array fields. Fix: the *bucket* or *subset* pattern, or moving the elements to another collection.

## 13. Counts on large collections

`countDocuments(filter)` runs an aggregation and examines the documents that match the filter. For an approximate total of the collection, `estimatedDocumentCount()` uses metadata and is instant. For frequent filtered counts, keep precomputed counters.

## 14. Too many or unused indexes

Every index slows down writes and takes RAM (the *working set* should fit in memory). `{a: 1}` is redundant if `{a: 1, b: 1}` exists. Check `$indexStats` after a representative traffic period; hide with `hideIndex` before dropping.

Low-selectivity fields (booleans, statuses with 3–4 values) on their own in an index rarely help; they are useful as the first equality field in a compound index.

## 15. Data model that does not match the read pattern

In MongoDB the model is designed around how data is read. Signals: a `$lookup` in the most frequent query, an N+1 pattern (one query per item in a list), or documents that are always read together but stored separately. Options:

- **Embed** data that is always read together and has a one-to-few relationship.
- **Reference** unbounded one-to-many relationships or data that changes independently.
- **Duplicate** frequently read, read-only fields (*extended reference* pattern), accepting the cost of keeping them in sync.
- **Precompute** heavily queried aggregates (*computed* pattern).

These proposals change the application, not just the query: present them as a medium-term option, not as an immediate fix.
