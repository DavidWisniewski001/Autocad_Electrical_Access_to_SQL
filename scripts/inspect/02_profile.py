"""Full-scan data profile + key uniqueness checks + read throughput. Read-only. Output: out/profile_<db>.json"""
import pyodbc, json, os, time, sys, re, itertools
ROOT = r"C:\Users\david\Documents\AutoCade_E\SQL_DB"
OUT = os.path.join(ROOT, "scripts", "inspect", "out")
DBS = {"default_cat": "default_cat.mdb", "footprint_lookup": "footprint_lookup.accdb"}
NUM = re.compile(r"^[+-]?(\d+(\.\d*)?|\.\d+)([eE][+-]?\d+)?$")
COMMON = [["CATALOG"], ["MANUFACTURER", "CATALOG"], ["MANUFACTURER", "CATALOG", "ASSEMBLYCODE"],
          ["CATALOG", "ASSEMBLYCODE"], ["CATALOG", "ASSEMBLYCODE", "BLKNAME"], ["CATALOG", "ASSEMBLYCODE", "BLKNAM"],
          ["CATALOG", "BLKNAME"], ["CATALOG", "BLKNAM"], ["MANUFACTURER", "CATALOG", "ASSEMBLYCODE", "LEVELS"],
          ["MANUFACTURER", "CATALOG", "ASSEMBLYCODE", "PINLIST"], ["MANUFACTURER", "CATALOG", "ASSEMBLYCODE", "COILPINS", "PINLIST"],
          ["MANUFACTURER", "CATALOG", "ASSEMBLYCODE", "PINLIST", "PEER_PINLIST", "COILPINS", "PEER_COILPINS"]]

def connect(path):
    return pyodbc.connect(r"DRIVER={Microsoft Access Driver (*.mdb, *.accdb)};DBQ=%s;ReadOnly=1;Exclusive=0" % path,
                          readonly=True, autocommit=True)

def norm(v):
    return None if v is None else str(v).strip().upper()

def profile_table(cur, name, tinfo):
    cols = [c["name"] for c in tinfo["columns"]]
    sel = "SELECT " + ",".join("[%s]" % c for c in cols) + " FROM [%s]" % name
    n = len(cols)
    st = [dict(null=0, empty=0, maxlen=0, nonascii=0, lead_trail_ws=0, numeric_like=0, nonempty=0, ctrl=0, distinct=None) for _ in cols]
    rows = []
    errors = []
    t0 = time.time()
    try:
        cur.execute(sel)
        while True:
            try:
                b = cur.fetchmany(5000)
            except Exception as e:
                errors.append(str(e)[:200]); break
            if not b: break
            rows.extend(tuple(r) for r in b)
    except Exception as e:
        errors.append("execute: " + str(e)[:200])
    el = time.time() - t0
    for r in rows:
        for i, v in enumerate(r):
            s = st[i]
            if v is None: s["null"] += 1; continue
            if not isinstance(v, str):
                v = str(v)
            if v == "": s["empty"] += 1; continue
            s["nonempty"] += 1
            if len(v) > s["maxlen"]: s["maxlen"] = len(v)
            if not v.isascii(): s["nonascii"] += 1
            if v != v.strip(): s["lead_trail_ws"] += 1
            if NUM.match(v.strip()): s["numeric_like"] += 1
            if any(ord(ch) < 32 for ch in v): s["ctrl"] += 1
    # distinct per column (case-sensitive) - only cheap
    for i in range(n):
        if len(rows) <= 400000:
            st[i]["distinct"] = len({r[i] for r in rows})
    out = dict(rows_read=len(rows), read_secs=round(el, 3), errors=errors, cols={c: s for c, s in zip(cols, st)})
    idx = {c: i for i, c in enumerate(cols)}
    # duplicate full rows excluding the COUNTER column
    counters = [c["name"] for c in tinfo["columns"] if c["type"] == "COUNTER"]
    keep = [i for i, c in enumerate(cols) if c not in counters]
    seen = {}
    for r in rows:
        k = tuple(r[i] for i in keep); seen[k] = seen.get(k, 0) + 1
    out["dup_rows_excl_counter"] = sum(v - 1 for v in seen.values() if v > 1)
    out["distinct_rows_excl_counter"] = len(seen)
    # same but normalised (upper/strip) because Access text compare is case-insensitive
    seen2 = {}
    for r in rows:
        k = tuple(norm(r[i]) for i in keep); seen2[k] = seen2.get(k, 0) + 1
    out["dup_rows_excl_counter_ci"] = sum(v - 1 for v in seen2.values() if v > 1)
    # candidate keys
    cands = []
    for c in counters: cands.append([c])
    for ixn, ix in tinfo["indexes"].items():
        cands.append([c for _, c in sorted(ix["cols"])])
    for cm in COMMON:
        if all(c in idx for c in cm): cands.append(cm)
    uniq = []
    seen_c = set()
    for cm in cands:
        if tuple(cm) in seen_c: continue
        seen_c.add(tuple(cm))
        ii = [idx[c] for c in cm]
        ks = [tuple(r[i] for i in ii) for r in rows]
        ksn = [tuple(norm(r[i]) for i in ii) for r in rows]
        nulls = sum(1 for k in ks if any(x is None or x == "" for x in k))
        uniq.append(dict(cols=cm, rows=len(ks), distinct=len(set(ks)), distinct_ci_trim=len(set(ksn)), null_or_empty=nulls))
    out["key_checks"] = uniq
    return out

def throughput(cur, name, cols, batches=(1000, 5000, 20000)):
    res = []
    for bs in batches:
        cur.execute("SELECT " + ",".join("[%s]" % c for c in cols) + " FROM [%s]" % name)
        t0 = time.time(); n = 0
        while True:
            b = cur.fetchmany(bs)
            if not b: break
            n += len(b)
        el = time.time() - t0
        res.append(dict(batch=bs, rows=n, secs=round(el, 2), rows_per_sec=round(n / el)))
    # fetchall for comparison
    cur.execute("SELECT * FROM [%s]" % name)
    t0 = time.time(); n = len(cur.fetchall()); el = time.time() - t0
    res.append(dict(batch="fetchall", rows=n, secs=round(el, 2), rows_per_sec=round(n / el)))
    return res

def main():
    only = sys.argv[1:] or list(DBS)
    for key in only:
        sch = json.load(open(os.path.join(OUT, "schema_%s.json" % key)))
        cn = connect(os.path.join(ROOT, "data_files", DBS[key])); cur = cn.cursor()
        prof = {"tables": {}}
        if key == "default_cat":
            big = max(sch["tables"], key=lambda t: sch["tables"][t]["rows"])
            prof["throughput_table"] = big
            prof["throughput"] = throughput(cur, big, [c["name"] for c in sch["tables"][big]["columns"]])
            print("throughput", big, prof["throughput"], flush=True)
        t0 = time.time()
        for i, (n, t) in enumerate(sch["tables"].items()):
            prof["tables"][n] = profile_table(cur, n, t)
            if i % 20 == 0: print(key, i, n, round(time.time() - t0), flush=True)
        cn.close()
        json.dump(prof, open(os.path.join(OUT, "profile_%s.json" % key), "w"), indent=1)
        print(key, "done", round(time.time() - t0), "s")

main()
