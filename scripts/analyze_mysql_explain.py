#!/usr/bin/env python3
"""
Analyzes MySQL EXPLAIN output and detects performance signals.

Accepted formats (auto-detected):
  - EXPLAIN FORMAT=JSON
  - Tabular EXPLAIN (mysql client table with '|', or tab-separated)
  - Vertical EXPLAIN (\\G)
  - EXPLAIN ANALYZE / FORMAT=TREE (lines starting with '->')

Usage:
  python analyze_mysql_explain.py plan.txt [--json]
  cat plan.txt | python analyze_mysql_explain.py - [--json]

Standard library only.
"""
import json
import re
import sys

SEVERITY_ORDER = {"HIGH": 0, "MEDIUM": 1, "LOW": 2, "INFO": 3}
BIG_TABLE = 1000          # rows from which a full scan matters
HUGE_TABLE = 100_000
# Functions that, applied to a column, prevent the use of its index
FUNC_ON_COLUMN = re.compile(
    r"\b(year|month|day|date|date_format|lower|upper|substr|substring|left|right|"
    r"trim|cast|convert|concat|ifnull|coalesce|abs|round|floor|unix_timestamp|"
    r"to_days|weekday|hour)\s*\(\s*`", re.IGNORECASE)


def finding(sev, code, table, msg, hint=""):
    return {"severity": sev, "code": code, "table": table, "message": msg, "hint": hint}


def to_int(v, default=None):
    try:
        return int(float(str(v).replace(",", "")))
    except (TypeError, ValueError):
        return default


def to_float(v, default=None):
    try:
        return float(str(v).replace("%", ""))
    except (TypeError, ValueError):
        return default


# ---------------------------------------------------------------- shared per-table checks
def analyze_table_row(t):
    """t: normalized dict with table, type, possible_keys, key, rows, filtered, extra, select_type."""
    out = []
    name = t.get("table") or "?"
    access = (t.get("type") or "").strip()
    rows = t.get("rows")
    filtered = t.get("filtered")
    extra = (t.get("extra") or "").lower()
    pk = t.get("possible_keys")
    key = t.get("key")
    big = rows is not None and rows >= BIG_TABLE
    sev_scan = "HIGH" if (rows is None or rows >= BIG_TABLE) else "LOW"

    if access == "ALL":
        if pk and not key:
            out.append(finding(sev_scan, "INDEX_IGNORED", name,
                               f"Full table scan (~{rows} rows) even though candidate indexes exist: {pk}.",
                               "Likely causes: implicit type/collation conversion, low selectivity, "
                               "stale statistics. Run SHOW WARNINGS after EXPLAIN and ANALYZE TABLE."))
        else:
            out.append(finding(sev_scan, "FULL_SCAN", name,
                               f"Full table scan (~{rows} rows) with no usable index.",
                               "Check whether the condition is indexable (functions on columns, LIKE '%x') "
                               "or whether an index is missing."))
    elif access == "index":
        out.append(finding("MEDIUM" if big else "LOW", "FULL_INDEX_SCAN", name,
                           f"Scans the whole index '{key}' (~{rows} entries).",
                           "The index is usually being used only for sorting or covering, not for filtering."))

    if filtered is not None and filtered < 20 and big and access not in ("const", "system"):
        out.append(finding("MEDIUM", "LOW_FILTERED", name,
                           f"filtered={filtered:g}%: ~{rows} rows are read and only ~{int(rows * filtered / 100)} "
                           "pass the filter.",
                           "Part of the WHERE is evaluated after reading; a compound index could include it."))

    if "using join buffer" in extra:
        out.append(finding("HIGH", "JOIN_WITHOUT_INDEX", name,
                           "JOIN without an index on the join column (join buffer / hash join).",
                           "Create an index on this table's column used in the ON clause (usually the FK)."))
    if "using filesort" in extra:
        out.append(finding("HIGH" if (rows or 0) >= HUGE_TABLE else "MEDIUM", "FILESORT", name,
                           "Sort without an index (Using filesort).",
                           "An index with equality columns first and then the ORDER BY columns avoids the sort."))
    if "using temporary" in extra:
        out.append(finding("MEDIUM", "TEMPORARY_TABLE", name,
                           "Temporary table (GROUP BY / DISTINCT / UNION).",
                           "An index starting with the GROUP BY columns can avoid it."))
    if "range checked for each record" in extra:
        out.append(finding("HIGH", "RANGE_CHECKED", name,
                           "'Range checked for each record': no suitable index for the join.", ""))
    if "impossible where" in extra:
        out.append(finding("INFO", "IMPOSSIBLE_WHERE", name, "The WHERE condition can never be true.", ""))
    if "using index" in extra and "using index condition" not in extra:
        out.append(finding("INFO", "COVERING_INDEX", name,
                           f"Covering index '{key}': the table is not read.", ""))

    st = (t.get("select_type") or "").upper()
    if "DEPENDENT" in st or t.get("dependent"):
        out.append(finding("HIGH", "DEPENDENT_SUBQUERY", name,
                           "Dependent subquery: it runs once per outer row.",
                           "Rewrite as a JOIN, EXISTS or derived table (beware of NOT IN with NULLs)."))

    cond = t.get("condition") or ""
    if cond and FUNC_ON_COLUMN.search(cond) and access in ("ALL", "index"):
        out.append(finding("HIGH", "FUNCTION_ON_COLUMN", name,
                           "The condition applies a function to a column: " + cond[:160],
                           "Isolate the column (date range instead of YEAR()) or use a functional index (8.0.13+)."))
    return out


# ---------------------------------------------------------------- JSON
def walk_json(node, tables, flags, ctx=None):
    ctx = ctx or {}
    if isinstance(node, dict):
        local = dict(ctx)
        if node.get("dependent") is True:
            local["dependent"] = True
        if node.get("using_filesort"):
            flags.append("filesort")
        if node.get("using_temporary_table"):
            flags.append("temporary")
        tbl = node.get("table")
        if isinstance(tbl, dict) and "table_name" in tbl:
            extra = []
            if tbl.get("using_join_buffer"):
                extra.append(f"Using join buffer ({tbl['using_join_buffer']})")
            if tbl.get("using_index"):
                extra.append("Using index")
            if tbl.get("using_index_condition") or tbl.get("index_condition"):
                extra.append("Using index condition")
            if tbl.get("using_filesort"):
                extra.append("Using filesort")
            rows = to_int(tbl.get("rows_examined_per_scan"))
            if rows is None:
                rows = to_int(tbl.get("rows"))
            tables.append({
                "table": tbl.get("table_name"),
                "type": tbl.get("access_type"),
                "possible_keys": ",".join(tbl.get("possible_keys", [])) or None,
                "key": tbl.get("key"),
                "used_key_parts": tbl.get("used_key_parts"),
                "rows": rows,
                "filtered": to_float(tbl.get("filtered")),
                "extra": "; ".join(extra),
                "condition": tbl.get("attached_condition", ""),
                "dependent": local.get("dependent", False),
            })
        for v in node.values():
            walk_json(v, tables, flags, local)
    elif isinstance(node, list):
        for v in node:
            walk_json(v, tables, flags, ctx)


def parse_json(text):
    data = json.loads(text)
    # Some clients return [{"EXPLAIN": "..."}] or a string with escaped JSON
    if isinstance(data, list) and data and isinstance(data[0], dict) and "EXPLAIN" in data[0]:
        data = json.loads(data[0]["EXPLAIN"])
    tables, flags = [], []
    walk_json(data, tables, flags)
    cost = None
    qb = data.get("query_block", {}) if isinstance(data, dict) else {}
    if isinstance(qb, dict):
        cost = qb.get("cost_info", {}).get("query_cost")
    # Query-level flags (ordering_operation / grouping_operation) are attached to the first table
    if tables:
        for f in set(flags):
            parts = [x for x in tables[0]["extra"].split("; ") if x]
            if f == "filesort" and "Using filesort" not in parts:
                parts.append("Using filesort")
            if f == "temporary" and "Using temporary" not in parts:
                parts.append("Using temporary")
            tables[0]["extra"] = "; ".join(parts)
    return tables, {"query_cost": cost}


# ---------------------------------------------------------------- tabular / vertical
def normalize_row(r):
    def clean(v):
        v = (v or "").strip()
        return None if v.upper() in ("NULL", "") else v
    return {
        "table": clean(r.get("table")),
        "select_type": clean(r.get("select_type")),
        "type": clean(r.get("type")),
        "possible_keys": clean(r.get("possible_keys")),
        "key": clean(r.get("key")),
        "key_len": clean(r.get("key_len")),
        "rows": to_int(clean(r.get("rows"))),
        "filtered": to_float(clean(r.get("filtered"))),
        "extra": clean(r.get("extra")) or "",
    }


def parse_vertical(text):
    rows, cur = [], None
    for line in text.splitlines():
        if re.match(r"\*+\s*\d+\.\s*row\s*\*+", line.strip()):
            if cur:
                rows.append(cur)
            cur = {}
            continue
        m = re.match(r"\s*([A-Za-z_]+):\s?(.*)$", line)
        if m and cur is not None:
            cur[m.group(1).lower()] = m.group(2)
    if cur:
        rows.append(cur)
    return [normalize_row(r) for r in rows]


def parse_tabular(text):
    lines = [l for l in text.splitlines() if l.strip() and not set(l.strip()) <= set("+-")]
    if not lines:
        return []
    sep = "|" if "|" in lines[0] else "\t"
    split = (lambda l: [c.strip() for c in l.strip().strip("|").split("|")]) if sep == "|" \
        else (lambda l: [c.strip() for c in l.split("\t")])
    header = [h.lower() for h in split(lines[0])]
    out = []
    for l in lines[1:]:
        cells = split(l)
        if len(cells) != len(header):
            continue
        out.append(normalize_row(dict(zip(header, cells))))
    return out


# ---------------------------------------------------------------- ANALYZE / TREE
TREE_RE = re.compile(r"->\s*(?P<op>.*?)\s*(?:\(cost=(?P<cost>[\d.e+]+)\s+rows=(?P<est>[\d.e+]+)\))?"
                     r"\s*(?:\(actual time=(?P<t1>[\d.]+)\.\.(?P<t2>[\d.]+)\s+rows=(?P<rows>[\d.e+]+)"
                     r"\s+loops=(?P<loops>\d+)\))?\s*$")


def parse_tree(text):
    findings, nodes = [], []
    for line in text.splitlines():
        if "->" not in line:
            continue
        m = TREE_RE.search(line)
        if not m:
            continue
        op = m.group("op")
        est = to_float(m.group("est"))
        actual = None
        if m.group("rows") is not None:
            actual = to_float(m.group("rows")) * int(m.group("loops"))
        time_ms = to_float(m.group("t2"))
        loops = to_int(m.group("loops"), 1)
        nodes.append({"op": op, "est_rows": est, "actual_rows": actual,
                      "time_last_ms": time_ms, "loops": loops})
        tbl = re.search(r"\bon (\w+)", op)
        tname = tbl.group(1) if tbl else "?"
        size = actual if actual is not None else est
        if op.startswith("Table scan"):
            sev = "HIGH" if (size or 0) >= BIG_TABLE else "LOW"
            msg = f"Full table scan on {tname}"
            if loops and loops > 1:
                sev = "HIGH"
                msg += f", repeated {loops} times (inside a loop)"
            findings.append(finding(sev, "FULL_SCAN", tname, msg + f" (~{int(size or 0)} rows in total).",
                                    "Check whether the condition is indexable or whether an index is missing."))
        elif op.startswith("Index scan"):
            findings.append(finding("MEDIUM" if (size or 0) >= BIG_TABLE else "LOW", "FULL_INDEX_SCAN", tname,
                                    f"Scans a whole index: {op[:120]}", ""))
        elif "hash join" in op.lower():
            findings.append(finding("HIGH", "JOIN_WITHOUT_INDEX", "?",
                                    f"{op[:80]}: the join cannot use an index.",
                                    "Create an index on the join column of the inner table."))
        elif op.startswith("Sort"):
            findings.append(finding("HIGH" if (size or 0) >= HUGE_TABLE else "MEDIUM", "FILESORT", "?",
                                    f"Sort without an index: {op[:120]}", ""))
        elif "temporary table" in op.lower():
            findings.append(finding("MEDIUM", "TEMPORARY_TABLE", "?", f"Temporary table: {op[:120]}", ""))
        elif op.startswith("Filter") and re.search(r"\b(year|date|lower|upper|cast|substr\w*)\s*\(", op, re.I):
            findings.append(finding("HIGH", "FUNCTION_ON_COLUMN", "?",
                                    f"Filter with a function applied to a column: {op[:160]}",
                                    "Isolate the column or use a functional index."))
        if est and actual is not None and loops == 1:
            ratio = max(est, 1) / max(actual, 1)
            if (ratio > 10 or ratio < 0.1) and max(est, actual) >= BIG_TABLE:
                findings.append(finding("MEDIUM", "BAD_ESTIMATE", tname,
                                        f"Estimated {int(est)} rows versus {int(actual)} actual in '{op[:60]}'.",
                                        "Skewed statistics: ANALYZE TABLE or histograms."))
    total_ms = nodes[0]["time_last_ms"] if nodes else None
    return findings, {"actual_time_ms_root": total_ms, "nodes": len(nodes)}


# ---------------------------------------------------------------- main
def sort_f(fs):
    return sorted(fs, key=lambda f: SEVERITY_ORDER.get(f["severity"], 9))


def detect_and_analyze(text):
    s = text.strip()
    if s.startswith("{") or s.startswith("["):
        tables, meta = parse_json(s)
        fmt = "json"
    elif re.search(r"^\s*->", s, re.M):
        findings, meta = parse_tree(s)
        return {"format": "tree/analyze", "meta": meta, "tables": [], "findings": sort_f(findings)}
    elif re.search(r"\*+\s*\d+\.\s*row\s*\*+", s):
        tables, meta, fmt = parse_vertical(s), {}, "vertical"
    else:
        tables, meta, fmt = parse_tabular(s), {}, "tabular"
    findings = []
    for t in tables:
        findings.extend(analyze_table_row(t))
    # Rough estimate of rows examined in a nested loop: each table is read once per row
    # coming out of the previous ones (rows × filtered%).
    prefix, total = 1.0, 0.0
    for t in tables:
        r = t.get("rows") or 1
        # With a hash join / join buffer the table is read only once, not once per previous row
        total += r if "join buffer" in (t.get("extra") or "").lower() else prefix * r
        prefix *= max(1.0, r * (t.get("filtered") or 100) / 100)
    meta["approx_rows_examined"] = int(total) if tables else None
    return {"format": fmt, "meta": meta, "tables": tables, "findings": sort_f(findings)}


def render(result):
    out = [f"Detected format: {result['format']}"]
    for k, v in result["meta"].items():
        if v is not None:
            out.append(f"{k}: {v}")
    if result["tables"]:
        out.append("\nTables in the plan:")
        for t in result["tables"]:
            out.append(f"  - {t['table']}: type={t.get('type')} key={t.get('key')} rows={t.get('rows')} "
                       f"filtered={t.get('filtered')} extra='{t.get('extra')}'")
    out.append("\nFindings:")
    if not result["findings"]:
        out.append("  (no problem signals detected)")
    for f in result["findings"]:
        out.append(f"  [{f['severity']}] {f['code']} ({f['table']}): {f['message']}")
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
        result = detect_and_analyze(text)
    except json.JSONDecodeError as e:
        print(f"Could not parse the plan JSON: {e}", file=sys.stderr)
        sys.exit(1)
    print(json.dumps(result, indent=2, ensure_ascii=False) if as_json else render(result))


if __name__ == "__main__":
    main()
