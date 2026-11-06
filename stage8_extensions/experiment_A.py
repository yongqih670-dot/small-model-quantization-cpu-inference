"""Paired cold-process startup with backend warmup enabled/disabled."""
import http.client
import json
from common import GEN, INPUT, attempt, clock, finish, is_pilot, messages, prepare, server

def stream(port,prompt):
    payload=dict(**GEN,messages=messages(prompt),max_tokens=96,stream=True)
    data=json.dumps(payload,ensure_ascii=False).encode("utf-8")
    conn=http.client.HTTPConnection("127.0.0.1",port,timeout=120)
    events=[]
    first=None
    try:
        start=clock()
        conn.request("POST","/v1/chat/completions",data,{"Content-Type":"application/json"})
        res=conn.getresponse()
        if res.status!=200:
            raise ValueError((res.status,res.read()[:300]))
        for raw in res:
            now=clock()
            if not raw.startswith(b"data:"):
                continue
            value=raw[5:].strip()
            if value==b"[DONE]":
                done=now
                break
            chunk=json.loads(value)
            events.append(dict(ns=now,chunk=chunk))
            for choice in chunk.get("choices",[]):
                if first is None and (choice["delta"].get("content") or "").strip():
                    first=now
        else:
            raise ValueError("Stream ended without DONE")
        if first is None:
            raise ValueError("No visible content")
        return dict(payload=payload,start_ns=start,first_ns=first,done_ns=done,events=events)
    finally:
        conn.close()

def main():
    pilot=is_pilot()
    pairs=[[True,False] if i%2==0 else [False,True] for i in range(1 if pilot else 6)]
    out,meta=prepare("A",pilot,dict(pairs=pairs,steps=["dev-001","dev-002"],poll_seconds=.010))
    rows=[]
    for r,pair in enumerate(pairs,1):
        for pos,warm in enumerate(pair,1):
            label=f"A{r}_{pos}"
            def collect(row):
                with server(out,label,"Q4_K_M",warm) as (port,sv):
                    first=stream(port,INPUT["first"])
                    row["first"]=first
                    second=stream(port,INPUT["second"])
                    row["second"]=second
                    row["startup_ready_ms"]=(sv["ready_ns"]-sv["start_ns"])/1e6
                    row["startup_text_ms"]=(first["first_ns"]-sv["start_ns"])/1e6
                    for name,resp in (("first",first),("second",second)):
                        row[name+"_ttft_ms"]=(resp["first_ns"]-resp["start_ns"])/1e6
                        row[name+"_total_ms"]=(resp["done_ns"]-resp["start_ns"])/1e6
            rows.append(attempt(out,label,dict(round=r,position=pos,warmup=warm),collect))
    return finish(out,meta,rows,len(pairs)*2)

if __name__=="__main__":
    raise SystemExit(main())
