#!/usr/bin/env python3
"""
Analyzes MongoDB explain() output and detects performance signals.

Accepts find() and aggregate(), classic and SBE (7.0+) engines, and sharded cluster plans.
The ideal input is JSON:  EJSON.stringify(db.c.find(...).explain("executionStats"))
It also tries to read raw mongosh output (unquoted keys, NumberLong(...), ISODate(...)).

Usage:
  python analyze_mongo_explain.py plan.json [--json]
  cat plan.json | python analyze_mongo_explain.py - [--json]

Standard library only.
"""
import json
import re
import sys

SEVERITY_ORDER = {"HIGH": 0, "MEDIUM": 1, "LOW": 2, "INFO": 3}
BIG = 1000
SKIP_KEYS = {"rejectedPlans", "allPlansExecution", "slotBasedPlan"}


def finding(sev, code, msg, hint=""):
    return {"severity": sev, "code": code, "message": msg, "hint": hint}


# ---------------------------------------------------------------- tolerant loading
def load(text):
    text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    t = text
    # mongosh types -> JSON values
    t = re.sub(r"(?:NumberLong|NumberInt|Long|Int32|NumberDecimal|Decimal128|Double)\(\s*['\"]?(-?[\d.eE+]+)['\"]?\s*\)", r"\1", t)
    t = re.sub(r"(?:ISODate|ObjectId|UUID|BinData\(\d+,)\s*\(?\s*(['\"][^'\"]*['\"])\s*\)", r"\1", t)
    t = re.sub(r"Timestamp\(\s*\{[^}]*\}\s*\)", "0", t)
    t = re.sub(r"Timestamp\(\s*\d+\s*,\s*\d+\s*\)", "0", t)
    t = re.sub(r"/((?:[^/\\\n]|\\.)+)/[gimsux]*", lambda m: json.dumps(m.group(0)), t)
    t = re.sub(r"'((?:[^'\\]|\\.)*)'", lambda m: json.dumps(m.group(1)), t)
    t = re.sub(r"([{,]\s*)([A-Za-z_$][\w$.]*)\s*:", r'\1"\2":', t)
    t = re.sub(r",\s*([}\]])", r"\1", t)
    return json.loads(t)


def num(v):
    """Converts numbers, including Extended JSON ({"$numberLong": "5"})."""
    if isinstance(v, dict):
        for k in ("$numberLong", "$numberInt", "$numberDouble", "$numberDecimal"):
            if k in v:
                v = v[k]
                break
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------- traversal
def find_all(node, key, acc=None):
    """All values of `key` in the document, without descending into rejected plans."""
    acc = [] if acc is None else acc
    if isinstance(node, dict):
        for k, v in node.items():
            if k in SKIP_KEYS:
                continue
            if k == key:
                acc.append(v)
            find_all(v, key, acc)
    elif isinstance(node, list):
        for v in node:
            find_all(v, key, acc)
    return acc


def stages(node, acc=None):
    """All stages (dicts with a 'stage' key) in a plan tree."""
    acc = [] if acc is None else acc
    if isinstance(node, dict):
        if "stage" in node and isinstance(node["stage"], str):
            acc.append(node)
        for k, v in node.items():
            if k not in SKIP_KEYS:
                stages(v, acc)
    elif isinstance(node, list):
        for v in node:
            stages(v, acc)
    return acc


def is_full_bounds(bounds):
    if not isinstance(bounds, dict) or not bounds:
        return False
    first = next(iter(bounds.values()))
    return isinstance(first, list) and any("MinKey" in str(b) and "MaxKey" in str(b) for b in first)


# ---------------------------------------------------------------- analysis
def analyze(doc):
    findings, summary = [], {}

    exec_stats = [e for e in find_all(doc, "executionStats") if isinstance(e, dict)]
    planners = [p for p in find_all(doc, "queryPlanner") if isinstance(p, dict)]

    if not exec_stats:
        findings.append(finding("INFO", "NO_EXEC_STATS",
                                "The plan has no executionStats: only estimates are available.",
                                "Re-run with .explain(\"executionStats\") to see documents examined and timings."))

    # Global metrics (summed when there are several, e.g. shards or $cursor)
    n = keys = docs = 0.0
    tms = None
    for e in exec_stats:
        n += num(e.get("nReturned")) or 0
        keys += num(e.get("totalKeysExamined")) or 0
        docs += num(e.get("totalDocsExamined")) or 0
        t = num(e.get("executionTimeMillis"))
        if t is not None:
            tms = max(tms or 0, t)
    if exec_stats:
        summary.update({"nReturned": int(n), "totalKeysExamined": int(keys),
                        "totalDocsExamined": int(docs), "executionTimeMillis": tms})
        denom = max(n, 1)
        rd, rk = docs / denom, keys / denom
        summary["ratio_docs_returned"] = round(rd, 1)
        summary["ratio_keys_returned"] = round(rk, 1)
        if docs >= BIG and rd > 100:
            findings.append(finding("HIGH", "HIGH_DOCS_RATIO",
                                    f"Examines {int(docs)} documents to return {int(n)} (ratio {rd:.0f}:1).",
                                    "The filter is not backed by a selective index."))
        elif docs >= BIG and rd > 10:
            findings.append(finding("MEDIUM", "HIGH_DOCS_RATIO",
                                    f"Examines {int(docs)} documents to return {int(n)} (ratio {rd:.0f}:1).", ""))
        if keys >= BIG and rk > 10 and rk > rd * 2:
            findings.append(finding("MEDIUM", "HIGH_KEYS_RATIO",
                                    f"Reads {int(keys)} index keys to return {int(n)} (ratio {rk:.0f}:1).",
                                    "The index scans too wide a range: check the ESR field order."))
        if docs == 0 and n > 0:
            findings.append(finding("INFO", "COVERED_QUERY", "Covered query: no documents are read.", ""))

    # Stage tree. With SBE (explainVersion "2") executionStages uses lowercase internal stages
    # (scan, ixseek, nlj...), so readable stage names come from winningPlan.queryPlan.
    sbe = any(isinstance(p.get("winningPlan"), dict) and "queryPlan" in p["winningPlan"] for p in planners)
    summary["engine"] = "sbe" if sbe else "classic"
    tree = []
    if not sbe:
        for e in exec_stats:
            tree += stages(e.get("executionStages", {}))
    if not tree:
        for p in planners:
            tree += stages(p.get("winningPlan", {}))
    names = [s["stage"] for s in tree]
    summary["stages"] = names

    for s in tree:
        st = s["stage"]
        sdocs = num(s.get("docsExamined"))
        if sdocs is None and exec_stats and names.count("COLLSCAN") == 1 and "EQ_LOOKUP" not in names:
            sdocs = docs
        if st == "COLLSCAN":
            big = sdocs is None or sdocs >= BIG
            findings.append(finding("HIGH" if big else "LOW", "COLLSCAN",
                                    "Full collection scan"
                                    + (f" ({int(sdocs)} documents)." if sdocs is not None else "."),
                                    "Create an index for the filter following ESR (equality, sort, range)."))
        elif st == "IXSCAN":
            idx = s.get("indexName") or s.get("keyPattern")
            if is_full_bounds(s.get("indexBounds")):
                findings.append(finding("MEDIUM", "UNBOUNDED_IXSCAN",
                                        f"Index {idx} is scanned unbounded on its first field [MinKey, MaxKey].",
                                        "It is probably used only for sorting; the filter does not use the index."))
            if s.get("isMultiKey"):
                findings.append(finding("INFO", "MULTIKEY", f"Index {idx} is multikey (includes an array).",
                                        "It cannot cover queries and range bounds may be looser."))
        elif st == "FETCH" and s.get("filter"):
            findings.append(finding("MEDIUM", "FETCH_FILTER",
                                    "Some predicates are evaluated after reading the document: "
                                    + json.dumps(s["filter"], ensure_ascii=False)[:150],
                                    "Adding those fields to the compound index would avoid reading discarded documents."))
        elif st in ("SORT", "sort"):
            disk = s.get("usedDisk") or (num(s.get("spills")) or 0) > 0
            findings.append(finding("HIGH" if disk else "MEDIUM", "IN_MEMORY_SORT",
                                    "In-memory sort" + (" that spilled to disk." if disk else "."),
                                    "An index including the sort() fields after the equality fields avoids this stage."))
        elif st == "SKIP":
            sk = num(s.get("skipAmount")) or 0
            if sk >= BIG:
                findings.append(finding("MEDIUM", "LARGE_SKIP", f"skip({int(sk)}): discards many documents.",
                                        "Use range-based pagination on the sort key."))
        elif st == "EQ_LOOKUP":
            strategy = s.get("strategy", "")
            if strategy == "NestedLoopJoin":
                findings.append(finding("HIGH", "LOOKUP_NO_INDEX",
                                        f"$lookup with NestedLoopJoin on '{s.get('foreignCollection', '?')}' "
                                        f"(foreignField '{s.get('foreignField', '?')}'): scans the foreign "
                                        "collection for every document.",
                                        "Create an index on foreignField."))
            elif strategy:
                findings.append(finding("INFO", "LOOKUP_STRATEGY", f"$lookup with strategy {strategy}.", ""))
        elif st == "GROUP" and (s.get("usedDisk") or (num(s.get("spills")) or 0) > 0):
            findings.append(finding("MEDIUM", "GROUP_SPILL", "$group spilled to disk due to memory limits.",
                                    "Filter before grouping or reduce the grouping keys."))
        elif st in ("OR", "SUBPLAN") and "COLLSCAN" in names:
            findings.append(finding("MEDIUM", "OR_COLLSCAN", "$or with at least one branch without an index.",
                                    "Every $or branch needs its own index."))

    # Classic-engine aggregation stages ('stages' list)
    for st_list in find_all(doc, "stages"):
        if not isinstance(st_list, list):
            continue
        for entry in st_list:
            if not isinstance(entry, dict):
                continue
            op = next((k for k in entry if k.startswith("$")), None)
            if op == "$lookup":
                scans = num(entry.get("collectionScans")) or 0
                ex = num(entry.get("totalDocsExamined")) or 0
                if scans > 0:
                    spec = entry["$lookup"] if isinstance(entry["$lookup"], dict) else {}
                    fc = spec.get("from", "?")
                    ff = spec.get("foreignField", "?")
                    findings.append(finding("HIGH", "LOOKUP_NO_INDEX",
                                            f"$lookup on '{fc}' performed {int(scans)} full collection scans "
                                            f"({int(ex)} documents examined).",
                                            f"Create an index on {fc}.{ff}."))
            elif op in ("$group", "$sort") and (entry.get("usedDisk") or (num(entry.get("spills")) or 0) > 0):
                findings.append(finding("MEDIUM", "STAGE_SPILL", f"{op} spilled to disk.", ""))
            if op and op != "$cursor":
                summary.setdefault("pipeline_after_cursor", []).append(op)

    rejected = sum(len(p.get("rejectedPlans", []) or []) for p in planners)
    if rejected:
        summary["rejectedPlans"] = rejected
        if rejected >= 3:
            findings.append(finding("INFO", "MANY_CANDIDATES",
                                    f"{rejected} rejected plans: several indexes compete with each other.",
                                    "Check whether any of them is redundant."))

    # Remove exact duplicates (e.g. several shards with the same problem)
    seen, uniq = set(), []
    for f in findings:
        k = (f["code"], f["message"])
        if k not in seen:
            seen.add(k)
            uniq.append(f)
    uniq.sort(key=lambda f: SEVERITY_ORDER.get(f["severity"], 9))
    return {"summary": summary, "findings": uniq}


def render(r):
    out = ["Summary:"]
    for k, v in r["summary"].items():
        out.append(f"  {k}: {v}")
    out.append("\nFindings:")
    if not r["findings"]:
        out.append("  (no problem signals detected)")
    for f in r["findings"]:
        out.append(f"  [{f['severity']}] {f['code']}: {f['message']}")
        if f["hint"]:
            out.append(f"      -> {f['hint']}")
    return "\n".join(out)


def main():
    args = [a for a in sys.argv[1:] if a != "--json"]
    as_json = "--json" in sys.argv
    if not args or args[0] == "-":
        text = sys.stdin.read()
    else:
        with open(args[0], encoding="utf-8") as fh:
            text = fh.read()
    try:
        doc = load(text)
    except json.JSONDecodeError as e:
        print("Could not parse the plan. Produce JSON with EJSON.stringify(...explain(\"executionStats\")). "
              f"Details: {e}", file=sys.stderr)
        sys.exit(1)
    result = analyze(doc)
    print(json.dumps(result, indent=2, ensure_ascii=False) if as_json else render(result))


if __name__ == "__main__":
    main()
