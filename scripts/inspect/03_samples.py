"""Samples and cross-checks used to confirm table meaning. Read-only."""
import pyodbc, json, os, collections
ROOT = r"C:\Users\david\Documents\AutoCade_E\SQL_DB"
def conn(f): return pyodbc.connect(r"DRIVER={Microsoft Access Driver (*.mdb, *.accdb)};DBQ=%s;ReadOnly=1;Exclusive=0"%os.path.join(ROOT,"data_files",f),readonly=True,autocommit=True).cursor()
c=conn("default_cat.mdb")
def show(q,n=3):
    print(">>",q)
    r=c.execute(q); cols=[d[0] for d in r.description]
    for row in r.fetchmany(n): print({k:(str(v)[:60] if v is not None else None) for k,v in zip(cols,row) if v is not None})
for t in ["CB","PB","_PINLIST_AB","_TERMPROPS_AB","_FAMILY_DESCRIPTION","_LISTBOX_DEF","_W0_CBLWIRES","_XREF_GRAPHICS","W0"]: show("SELECT * FROM [%s]"%t)
print("pinlist MANUFACTURER vs suffix:")
tabs=[r.table_name for r in c.tables(tableType="TABLE")]
bad=0
for t in tabs:
    if t.startswith("_PINLIST_") or t.startswith("_TERMPROPS_"):
        ms=[r[0] for r in c.execute("SELECT DISTINCT MANUFACTURER FROM [%s]"%t).fetchall()]
        if len(ms)!=1 or ms[0] is None or ms[0].upper()!=t.split("_",2)[2].upper(): print(" ",t,ms[:4]); bad+=1
print("mismatches",bad)
print("_LISTBOX_DEF TABLENAME distinct:",[r[0] for r in c.execute("SELECT DISTINCT TABLENAME FROM _LISTBOX_DEF")][:100])
print("_FAMILY_DESCRIPTION:",c.execute("SELECT FAMILY,DESC FROM _FAMILY_DESCRIPTION").fetchmany(80))
print("non-ascii samples:")
for t,col in [("CB","CATALOG"),("AM","DESCRIPTION"),("CB","DESCRIPTION")]:
    k=0
    for (v,) in c.execute("SELECT [%s] FROM [%s] WHERE [%s] IS NOT NULL"%(col,t,col)):
        if not v.isascii():
            print(" ",t,col,repr(v[:80]), [hex(ord(x)) for x in v if ord(x)>127][:5]); k+=1
            if k>=3: break
print("ctrl:", [repr(v[:60]) for (v,) in c.execute("SELECT DESCRIPTION FROM PX WHERE DESCRIPTION IS NOT NULL") if any(ord(x)<32 for x in v)][:2])
print("SYMBOL2D long:", c.execute("SELECT TOP 1 SYMBOL2D FROM SS WHERE SYMBOL2D IS NOT NULL").fetchall())
# table name vs MANUFACTURER in footprint
f=conn("footprint_lookup.accdb")
for t in ["AB","ALLEN BRADLEY","ALLEN-BRADLEY","SCE","_PNLMISC"]:
    r=f.execute("SELECT TOP 3 * FROM [%s]"%t); cols=[d[0] for d in r.description]
    print(t,[dict(zip(cols,x)) for x in r.fetchall()])
fd=f.execute("SELECT COUNT(*) FROM [ALLEN BRADLEY]").fetchone(),f.execute("SELECT COUNT(*) FROM [ALLEN-BRADLEY]").fetchone(),f.execute("SELECT COUNT(*) FROM [AB]").fetchone()
print("AB variants counts",fd)
# cross-DB: catalog numbers in footprint AB found in default_cat AB-ish tables?
fcat={r[0] for r in f.execute("SELECT CATALOG FROM [AB]")}
cat={r[0] for t in ["CB","MS","PB","CR","OL"] for r in c.execute("SELECT CATALOG FROM [%s] WHERE MANUFACTURER='AB'"%t)}
print("cross-db overlap AB footprint catalogs vs default_cat(AB, 5 tables):",len(fcat),len(cat),len(fcat&cat))
# dup examples
for t in ["SCE","SQD"]:
    r=f.execute("SELECT CATALOG,ASSEMBLYCODE,COUNT(*) FROM [%s] GROUP BY CATALOG,ASSEMBLYCODE HAVING COUNT(*)>1"%t).fetchmany(3); print(t,"dup groups",[tuple(x) for x in r])
