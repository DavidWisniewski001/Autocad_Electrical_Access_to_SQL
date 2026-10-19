"""Dump schema (tables, columns, statistics, primaryKeys, row counts) of ACADE Access DBs to JSON. Read-only."""
import pyodbc, json, sys, os, time, struct
ROOT = r"C:\Users\david\Documents\AutoCade_E\SQL_DB"
OUT = os.path.join(ROOT, "scripts", "inspect", "out")
os.makedirs(OUT, exist_ok=True)
DBS = {"default_cat": "default_cat.mdb", "footprint_lookup": "footprint_lookup.accdb"}

def connect(path):
    return pyodbc.connect(r"DRIVER={Microsoft Access Driver (*.mdb, *.accdb)};DBQ=%s;ReadOnly=1;Exclusive=0" % path,
                          readonly=True, autocommit=True)

def main():
    for key, fn in DBS.items():
        path = os.path.join(ROOT, "data_files", fn)
        cn = connect(path); cur = cn.cursor()
        res = {"file": fn, "python_bits": struct.calcsize("P") * 8, "tables": {}, "other_objects": [], "pk_support": None, "stat_support": None}
        types = {}
        for r in cur.tables():
            types.setdefault(r.table_type, []).append(r.table_name)
        res["table_types"] = {k: len(v) for k, v in types.items()}
        res["other_objects"] = [(t, n) for t, ns in types.items() if t != "TABLE" for n in ns]
        res["linked"] = [r.table_name for r in cur.tables(tableType="LINK")] if True else []
        names = sorted(types.get("TABLE", []))
        pkerr = None; staterr = None
        for n in names:
            cols = []
            for c in cur.columns(table=n):
                cols.append(dict(name=c.column_name, type=c.type_name, dtype=c.data_type, size=c.column_size,
                                 digits=c.decimal_digits, nullable=bool(c.nullable), default=c.column_def,
                                 buflen=c.buffer_length))
            t = dict(columns=cols, pk=[], indexes={}, rows=None)
            try:
                t["pk"] = [r.column_name for r in cur.primaryKeys(table=n)]
            except Exception as e:
                pkerr = str(e)[:200]
            try:
                for r in cur.statistics(table=n):
                    if r.index_name is None: continue
                    ix = t["indexes"].setdefault(r.index_name, dict(unique=not r.non_unique, cols=[]))
                    ix["cols"].append((r.ordinal_position, r.column_name))
            except Exception as e:
                staterr = str(e)[:200]
            t0 = time.time()
            try:
                t["rows"] = cur.execute("SELECT COUNT(*) FROM [%s]" % n).fetchone()[0]
            except Exception as e:
                t["rows_err"] = str(e)[:200]
            res["tables"][n] = t
        res["pk_error"] = pkerr; res["stat_error"] = staterr
        # MSys tables visible?
        try:
            res["msys"] = [r.table_name for r in cur.tables(tableType="SYSTEM TABLE")]
        except Exception as e:
            res["msys"] = "ERR " + str(e)[:100]
        cn.close()
        with open(os.path.join(OUT, "schema_%s.json" % key), "w") as f:
            json.dump(res, f, indent=1)
        print(key, len(names), res["table_types"], "pkerr", pkerr, "staterr", staterr)

main()
