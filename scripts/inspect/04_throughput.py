"""Throughput of full read of largest catalog table MS, several batch sizes, in two orders to expose caching effects. Read-only."""
import pyodbc, time, json, os
p=r"C:\Users\david\Documents\AutoCade_E\SQL_DB\data_files\default_cat.mdb"
res=[]
for bs in (20000,1000,5000,1000,20000,"all"):
    c=pyodbc.connect(r"DRIVER={Microsoft Access Driver (*.mdb, *.accdb)};DBQ=%s;ReadOnly=1;Exclusive=0"%p,readonly=True,autocommit=True).cursor()
    c.execute("SELECT * FROM [MS]"); t=time.time(); n=0
    if bs=="all": n=len(c.fetchall())
    else:
        while True:
            b=c.fetchmany(bs)
            if not b: break
            n+=len(b)
    el=time.time()-t; res.append((bs,n,round(el,2),round(n/el)))
    print(res[-1],flush=True)
json.dump(res,open(os.path.join(os.path.dirname(__file__),"out","throughput_fresh_conn.json"),"w"))
