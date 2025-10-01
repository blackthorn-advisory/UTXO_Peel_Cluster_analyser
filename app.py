#!/usr/bin/env python3
"""
UTXO Tracer - Flask UI (tabs per graph/table + label-toggle for graphs)

Usage:
  python app.py
Open: http://127.0.0.1:5000/

Dependencies:
  pip install flask requests networkx matplotlib
"""
import os, uuid, time, csv, math, shutil
from collections import defaultdict
from flask import Flask, request, render_template_string, send_from_directory, redirect, url_for, flash
import requests, networkx as nx

# HEADLESS matplotlib setup before pyplot import
os.environ.setdefault("MPLBACKEND", "Agg")
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

# Config
ESPLORA = os.environ.get("ESPLORA_API", "https://blockstream.info/api")
SLEEP = float(os.environ.get("ESPLORA_SLEEP", "0.25"))
OUTPUT_ROOT = "outputs"
os.makedirs(OUTPUT_ROOT, exist_ok=True)

# simple in-memory cache for address lookups during a run
ADDRESS_TXS_CACHE = {}
def _cached_address_txs(addr):
    """Return list of tx summaries for addr from Esplora, cached per-run."""
    if addr in ADDRESS_TXS_CACHE:
        return ADDRESS_TXS_CACHE[addr]
    try:
        r = requests.get(f"{ESPLORA}/address/{addr}/txs", timeout=12)
        r.raise_for_status()
        res = r.json() or []
    except Exception:
        res = []
    ADDRESS_TXS_CACHE[addr] = res
    return res


app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET", "week1tracer-secret")
app.jinja_env.filters['basename'] = os.path.basename

### ----------------- Tracer helpers (same as before) -----------------
def get_tx_json(txid):
    url = f"{ESPLORA}/tx/{txid}"
    r = requests.get(url, timeout=30)
    r.raise_for_status()
    return r.json()

def get_outspends(txid):
    url = f"{ESPLORA}/tx/{txid}/outspends"
    r = requests.get(url, timeout=30)
    r.raise_for_status()
    return r.json()

class UnionFind:
    def __init__(self): self.parent = {}
    def find(self, a):
        if a not in self.parent: self.parent[a] = a; return a
        if self.parent[a] == a: return a
        self.parent[a] = self.find(self.parent[a]); return self.parent[a]
    def union(self, a, b):
        ra=self.find(a); rb=self.find(b)
        if ra!=rb: self.parent[rb]=ra
    def groups(self):
        out=defaultdict(list)
        for k in list(self.parent.keys()): out[self.find(k)].append(k)
        return out

def detect_coinjoin(tx_json):
    vin_count=len(tx_json.get("vin",[])); vout_count=len(tx_json.get("vout",[]))
    if vin_count>=5 and vout_count>=5:
        values=[v.get("value",0) for v in tx_json.get("vout",[]) if v.get("value",0)>0]
        if not values: return False,0.0
        mean=sum(values)/len(values)
        variance=sum((x-mean)**2 for x in values)/len(values)
        sd=math.sqrt(variance); rel=sd/(mean+1e-9)
        score=max(0.0,1.0-min(rel/0.05,1.0))
        return score>0.6,score
    return False,0.0

def change_candidate_scores(tx_json):
    vin_addrs=[]; vin_script_types=[]
    for vin in tx_json.get("vin",[]):
        prev=vin.get("prevout") or {}
        if "scriptpubkey_address" in prev: vin_addrs.append(prev.get("scriptpubkey_address"))
        if "scriptpubkey_type" in prev: vin_script_types.append(prev.get("scriptpubkey_type"))
    vout_list=tx_json.get("vout",[])
    out_scores=[]
    majority_script=None
    if vin_script_types: majority_script=max(set(vin_script_types), key=vin_script_types.count)
    max_in=max([vin.get("prevout",{}).get("value",0) for vin in tx_json.get("vin",[])] + [0])
    for idx,vout in enumerate(vout_list):
        score=0.0; addr=vout.get("scriptpubkey_address"); sval=vout.get("value",0)
        if addr and addr in vin_addrs: score+=0.4
        if majority_script and vout.get("scriptpubkey_type")==majority_script: score+=0.2
        if max_in>0 and sval<max_in*0.95 and sval>0: score+=0.2
        out_scores.append({"vout_index":idx,"address":addr or "NON_STANDARD","value":sval,"score":round(min(score,1.0),4)})
    positive=[o for o in out_scores if o["score"]>0]
    if len(positive)==1: positive[0]["score"]=min(1.0,positive[0]["score"]+0.15)
    return out_scores

def add_bipartite_edges_for_tx(tx_json,bip_edges):
    txid=tx_json["txid"]
    for vin in tx_json.get("vin",[]):
        prev=vin.get("prevout") or {}
        addr=prev.get("scriptpubkey_address") or "UNKNOWN_INPUT"
        val=prev.get("value",0)
        bip_edges.append({"type":"addr->tx","from":addr,"to":f"tx:{txid}","sats":val,"txid":txid})
    for idx,vout in enumerate(tx_json.get("vout",[])):
        addr=vout.get("scriptpubkey_address") or f"NON_STD_VOUT_{idx}"
        val=vout.get("value",0)
        bip_edges.append({"type":"tx->addr","from":f"tx:{txid}","to":addr,"sats":val,"txid":txid})
    time.sleep(SLEEP)

def project_address_to_address(bip_edges):
    tx_inputs=defaultdict(list); tx_outputs=defaultdict(list)
    for e in bip_edges:
        if e["type"]=="addr->tx": tx_inputs[e["txid"]].append((e["from"],e["sats"]))
        else: tx_outputs[e["txid"]].append((e["to"],e["sats"]))
    projected=[]
    for txid in set(list(tx_inputs.keys())+list(tx_outputs.keys())):
        inputs=tx_inputs.get(txid,[]); outputs=tx_outputs.get(txid,[])
        total_in=sum([v for (_,v) in inputs]) or 1
        for out_addr,out_sats in outputs:
            for in_addr,in_sats in inputs:
                share=(in_sats/total_in)*out_sats
                if share<=0: continue
                projected.append({"txid":txid,"from":in_addr,"to":out_addr,"sats":int(round(share))})
    return projected

def sats_to_btc(sats): return sats/1e8

def trace_peel_chain(txid,vout_index=0,max_hops=8):
    chain=[]; cur_tx=txid; cur_vout=vout_index
    for hop in range(max_hops):
        try: outspends=get_outspends(cur_tx)
        except Exception as e: chain.append({"error":f"failed_outspends:{e}"}); break
        if cur_vout>=len(outspends): chain.append({"error":"vout_index_out_of_range"}); break
        out=outspends[cur_vout]; spent=out.get("spent",False); value=out.get("value",0)
        spent_txid=out.get("txid"); spent_vin=out.get("vin",None)
        spent_addr=None
        if spent and spent_txid:
            try:
                sp_tx=get_tx_json(spent_txid)
                outs=sp_tx.get("vout",[])
                if outs:
                    candidate=max(outs,key=lambda o:o.get("value",0))
                    spent_addr=candidate.get("scriptpubkey_address") or "NON_STD"
            except Exception: spent_addr=None
        chain.append({"from_tx":cur_tx,"from_vout":cur_vout,"value_sats":value,"spent":spent,"spent_in_tx":spent_txid,"spent_in_vin_index":spent_vin,"spent_addr":spent_addr})
        if not spent or not spent_txid: break
        cur_tx=spent_txid; cur_vout=0; time.sleep(SLEEP)
    return chain

def read_csv_preview(path, max_rows=500):
    if not os.path.exists(path): return {"columns":[],"rows":[]}
    rows=[]; cols=[]
    with open(path,newline='',encoding='utf-8') as fh:
        reader=csv.DictReader(fh)
        cols=reader.fieldnames or []
        for i,r in enumerate(reader):
            if i>=max_rows: break
            rows.append(r)
    return {"columns":cols,"rows":rows}

def _truncated_label(s, left=12, right=8):
    if s is None: return ""
    if len(s) <= left + right + 3: return s
    return s[:left] + "..." + s[-right:]

def process_txids(txid_list,outdir):
    os.makedirs(outdir,exist_ok=True)
    bip_edges=[]; uf=UnionFind(); tx_flags=[]
    for txid in txid_list:
        txid=txid.strip()
        if not txid: continue
        tx = get_tx_json(txid)
        cj_flag, cj_score = detect_coinjoin(tx)
        # Use the richer change candidate detector (includes novelty/script/round heuristics)
        change_scores = detect_change_candidates_for_tx(tx)
        tx_flags.append({
            "txid": txid,
            "coinjoin": cj_flag,
            "coinjoin_score": round(cj_score, 4),
            "change_scores": change_scores
        })

        inputs=[]
        for vin in tx.get("vin",[]):
            prev=vin.get("prevout") or {}
            addr=prev.get("scriptpubkey_address") or "UNKNOWN_INPUT"
            inputs.append(addr)
        if len(inputs)>=2:
            base=inputs[0]
            for other in inputs[1:]:
                uf.union(base,other)
        add_bipartite_edges_for_tx(tx,bip_edges)
    projected=project_address_to_address(bip_edges)

    bip_csv=os.path.join(outdir,"bipartite_edges.csv")
    with open(bip_csv,"w",newline="",encoding='utf-8') as fh:
        w=csv.DictWriter(fh,fieldnames=["type","from","to","sats","txid"]); w.writeheader()
        for r in bip_edges: w.writerow(r)
    proj_csv=os.path.join(outdir,"evidence_address_to_address.csv")
    with open(proj_csv,"w",newline="",encoding='utf-8') as fh:
        w=csv.DictWriter(fh,fieldnames=["txid","from","to","sats","btc"]); w.writeheader()
        for r in projected: w.writerow({"txid":r["txid"],"from":r["from"],"to":r["to"],"sats":r["sats"],"btc":sats_to_btc(r["sats"])})
    clusters=uf.groups()
    cl_csv=os.path.join(outdir,"clusters.csv")
    with open(cl_csv,"w",newline="",encoding='utf-8') as fh:
        w=csv.writer(fh); w.writerow(["cluster_root","member_address"])
        for root,members in clusters.items():
            for m in members: w.writerow([root,m])
    flags_csv=os.path.join(outdir,"tx_flags.csv")
    with open(flags_csv,"w",newline="",encoding='utf-8') as fh:
        w=csv.writer(fh); w.writerow(["txid","coinjoin","coinjoin_score","change_scores_json"])
        for t in tx_flags: w.writerow([t["txid"],t["coinjoin"],t["coinjoin_score"],str(t["change_scores"])])

    # bipartite graph (two versions: short and full labels)
    try:
        G=nx.DiGraph()
        for e in bip_edges:
            G.add_node(e["from"]); G.add_node(e["to"])
            wgt=e["sats"]/1e8
            G.add_edge(e["from"],e["to"],weight=wgt)
        # positions once for consistency/appearance
        pos = nx.spring_layout(G, k=0.7, iterations=120)

        # SHORT labels (truncated)
        plt.figure(figsize=(12,9))
        ns=[200+250*(G.degree(n)) for n in G.nodes()]
        nx.draw_networkx_nodes(G,pos,node_size=ns,node_color="#a6d8ff")
        nx.draw_networkx_edges(G,pos,arrowstyle="-|>",arrowsize=10,width=1)
        labels_short = {n: _truncated_label(n) for n in G.nodes()}
        nx.draw_networkx_labels(G,pos,labels_short,font_size=7)
        edge_labels = {(u,v):f"{d['weight']:.6f}" for u,v,d in G.edges(data=True)}
        nx.draw_networkx_edge_labels(G,pos,edge_labels=edge_labels,font_size=7)
        plt.title("Bipartite Address ↔ TX")
        plt.axis("off")
        bip_png = os.path.join(outdir,"bipartite_graph.png")
        plt.tight_layout(); plt.savefig(bip_png,dpi=150); plt.close()

        # FULL labels
        plt.figure(figsize=(12,9))
        nx.draw_networkx_nodes(G,pos,node_size=ns,node_color="#a6d8ff")
        nx.draw_networkx_edges(G,pos,arrowstyle="-|>",arrowsize=10,width=1)
        labels_full = {n: n for n in G.nodes()}
        nx.draw_networkx_labels(G,pos,labels_full,font_size=7)
        nx.draw_networkx_edge_labels(G,pos,edge_labels=edge_labels,font_size=7)
        plt.title("Bipartite Address ↔ TX — Full labels")
        plt.axis("off")
        bip_png_full = os.path.join(outdir,"bipartite_graph_full.png")
        plt.tight_layout(); plt.savefig(bip_png_full,dpi=150); plt.close()

    except Exception as e:
        print("Graph draw error:",e)

    # projected graph (two versions)
    try:
        G2=nx.DiGraph()
        for p in projected:
            a=p["from"]; b=p["to"]; w=sats_to_btc(p["sats"])
            if G2.has_edge(a,b): G2[a][b]["weight"]+=w
            else: G2.add_edge(a,b,weight=w)
        pos2 = nx.spring_layout(G2, k=0.6, iterations=150)
        ns2=[200+250*(G2.degree(n)) for n in G2.nodes()]

        # SHORT
        plt.figure(figsize=(12,9))
        nx.draw_networkx_nodes(G2,pos2,node_size=ns2,node_color="#b7f4c6")
        nx.draw_networkx_edges(G2,pos2,arrowstyle="-|>",arrowsize=10,width=1.2)
        labels2_short = {n: _truncated_label(n) for n in G2.nodes()}
        nx.draw_networkx_labels(G2,pos2,labels2_short,font_size=8)
        edge_labels2 = {(u,v):f"{d['weight']:.6f}" for u,v,d in G2.edges(data=True)}
        nx.draw_networkx_edge_labels(G2,pos2,edge_labels=edge_labels2,font_size=7)
        proj_png = os.path.join(outdir,"projected_graph.png")
        plt.title("Projected Address → Address (BTC)")
        plt.axis("off")
        plt.tight_layout(); plt.savefig(proj_png,dpi=150); plt.close()

        # FULL
        plt.figure(figsize=(12,9))
        nx.draw_networkx_nodes(G2,pos2,node_size=ns2,node_color="#b7f4c6")
        nx.draw_networkx_edges(G2,pos2,arrowstyle="-|>",arrowsize=10,width=1.2)
        labels2_full = {n: n for n in G2.nodes()}
        nx.draw_networkx_labels(G2,pos2,labels2_full,font_size=8)
        nx.draw_networkx_edge_labels(G2,pos2,edge_labels=edge_labels2,font_size=7)
        proj_png_full = os.path.join(outdir,"projected_graph_full.png")
        plt.title("Projected Address → Address — Full labels")
        plt.axis("off")
        plt.tight_layout(); plt.savefig(proj_png_full,dpi=150); plt.close()

    except Exception as e:
        print("Projected graph draw error:",e)

    return {
        "bip":bip_csv,"proj":proj_csv,"clusters":cl_csv,"flags":flags_csv,
        "bip_png":os.path.join(outdir,"bipartite_graph.png"),
        "bip_png_full":os.path.join(outdir,"bipartite_graph_full.png"),
        "proj_png":os.path.join(outdir,"projected_graph.png"),
        "proj_png_full":os.path.join(outdir,"projected_graph_full.png")
    }

### -------------------- Templates (RESULT_HTML includes toggle) --------------------
INDEX_HTML = """
<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <title>UTXO Tracer</title>
  <link href="https://fonts.googleapis.com/css2?family=Inter:wght@300;400;600;700&display=swap" rel="stylesheet">
  <style>
    body { font-family: 'Inter',system-ui,-apple-system,Segoe UI,Roboto,Helvetica,Arial; padding:18px; background:#f6f8fb; color:#111; }
    .card { background:#fff; border-radius:10px; padding:18px; box-shadow:0 6px 18px rgba(16,24,40,0.06); max-width:1100px; margin:10px auto; }
    textarea, input[type=text], input[type=file] { width:100%; padding:10px; border:1px solid #e6eef8; border-radius:8px; }
    button { background:#0b6ff2; color:#fff; border:none; padding:10px 14px; border-radius:8px; cursor:pointer; font-weight:600; }
    small.muted { color:#7c88a1; }
    footer { text-align:center; color:#8892a6; font-size:13px; margin-top:14px; }
  </style>
</head>
<body>
  <div class="card">
    <h2>UTXO Tracer</h2>
    <p class="muted">Paste one or more BTC txids (one per line) or upload a text file. Results include graphs, tables and downloadable CSVs.</p>
    <form method="post" action="/analyze" enctype="multipart/form-data">
      <label><strong>TXIDs (one per line)</strong></label>
      <textarea name="txids" rows="6" placeholder="paste txids here"></textarea>
      <div style="display:flex;gap:12px;margin-top:12px;">
        <div style="flex:1">
          <label><strong>or upload file</strong> (plaintext)</label>
          <input type="file" name="txfile" accept=".txt,text/plain"/>
        </div>
        <div style="width:260px">
          <label><strong>Optional label</strong></label>
          <input type="text" name="label" placeholder="Case 2025-09 - victim"/>
        </div>
      </div>
      <div style="margin-top:12px;">
        <button type="submit">Analyze</button>
      </div>
    </form>
  </div>
  <footer>Local tool — uses Blockstream public API by default. Set <code>ESPLORA_API</code> to use local indexer.</footer>
</body>
</html>
"""

RESULT_HTML = """
<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <title>UTXO Tracer — Results</title>
  <link href="https://fonts.googleapis.com/css2?family=Inter:wght@300;400;600;700&display=swap" rel="stylesheet">
  <style>
    body{font-family:'Inter',system-ui,-apple-system,Segoe UI,Roboto,Helvetica,Arial;background:#f6f8fb;padding:14px;color:#0b1726}
    .container{max-width:1200px;margin:10px auto}
    .header{display:flex;align-items:center;justify-content:space-between;gap:12px;margin-bottom:12px}
    .title {font-size:20px;font-weight:700}
    .meta {color:#5b6b84;font-size:13px}
    .run-badge{background:#eef6ff;border:1px solid #d6eaff;padding:6px 10px;border-radius:999px;color:#0b6ff2;font-weight:600}
    .tabs {display:flex;gap:8px;flex-wrap:wrap;margin-bottom:10px}
    .tab {padding:8px 12px;border-radius:8px;background:#fff;border:1px solid #e6eef8;cursor:pointer;box-shadow:0 1px 0 rgba(11,22,40,0.02)}
    .tab.active{background:linear-gradient(180deg,#eaf3ff,#dff0ff);border-color:#bfe0ff;box-shadow:0 8px 24px rgba(11,111,242,0.08)}
    .panel {background:#fff;border-radius:10px;padding:14px;box-shadow:0 6px 18px rgba(16,24,40,0.04)}
    .downloads ul{margin:0;padding-left:18px}
    .tables {display:grid;grid-template-columns:1fr;gap:16px}
    .table-wrap{overflow:auto;max-height:520px;border:1px solid #f0f4fb;padding:8px;background:#fff;border-radius:6px}
    .csv-table{border-collapse:collapse;width:100%;font-size:13px}
    .csv-table th,.csv-table td{border:1px solid #f0f4fb;padding:8px 10px;text-align:left;vertical-align:top}
    .csv-table th{position:sticky;top:0;background:#fbfdff;font-weight:700}
    img.graph{max-width:100%;border-radius:8px;border:1px solid #eef6ff}
    .toggle { display:flex; align-items:center; gap:10px; margin-bottom:10px }
    .toggle input { transform:scale(1.1); }
    a.small {color:#0b6ff2;font-weight:600}
  </style>
</head>
<body>
  <div class="container">
    <div class="header">
      <div>
        <div class="title">UTXO Tracer — Results</div>
        <div class="meta">Label: <strong>{{ label or '(none)' }}</strong> &nbsp; <span style="color:#9aa6bb">•</span> &nbsp; <span class="meta">Run: <span class="run-badge">{{ run_id }}</span></span></div>
      </div>
      <div>
        <a href="{{ url_for('index') }}" class="tab" style="background:#fff;border-radius:8px;padding:8px 12px">Run new analysis</a>
      </div>
    </div>

    <div class="tabs" role="tablist">
      <div class="tab active" data-target="downloads">Downloads</div>
      <div class="tab" data-target="bipartite_graph">Bipartite Graph</div>
      <div class="tab" data-target="projected_graph">Projected Graph</div>
      <div class="tab" data-target="bip_table">Bipartite Table</div>
      <div class="tab" data-target="proj_table">Projected Table</div>
      <div class="tab" data-target="clusters_table">Clusters</div>
      <div class="tab" data-target="flags_table">Tx Flags</div>
    </div>

    <div id="downloads" class="panel tab-content active">
      <h3>Downloads</h3>
      <div class="downloads">
        <ul>
          <li><a class="small" href="{{ url_for('download', run_id=run_id, filename=paths['bip']|basename) }}">bipartite_edges.csv</a></li>
          <li><a class="small" href="{{ url_for('download', run_id=run_id, filename=paths['proj']|basename) }}">evidence_address_to_address.csv</a></li>
          <li><a class="small" href="{{ url_for('download', run_id=run_id, filename=paths['clusters']|basename) }}">clusters.csv</a></li>
          <li><a class="small" href="{{ url_for('download', run_id=run_id, filename=paths['flags']|basename) }}">tx_flags.csv</a></li>
          <li><a class="small" href="{{ url_for('download', run_id=run_id, filename=paths['bip_png']|basename) }}">bipartite_graph (short labels).png</a></li>
          <li><a class="small" href="{{ url_for('download', run_id=run_id, filename=paths['bip_png_full']|basename) }}">bipartite_graph (full labels).png</a></li>
          <li><a class="small" href="{{ url_for('download', run_id=run_id, filename=paths['proj_png']|basename) }}">projected_graph (short labels).png</a></li>
          <li><a class="small" href="{{ url_for('download', run_id=run_id, filename=paths['proj_png_full']|basename) }}">projected_graph (full labels).png</a></li>
        </ul>
      </div>
    </div>

    <div id="bipartite_graph" class="panel tab-content" style="display:none">
      <div class="toggle">
        <label><input type="checkbox" id="bip_full_toggle"> Show full labels</label>
        <div style="color:#66788f;font-size:13px">Toggle to show full addresses / txids on the graph (may be crowded).</div>
      </div>
      <img id="bip_img" class="graph" src="{{ url_for('download', run_id=run_id, filename=paths['bip_png']|basename) }}"
           data-short="{{ url_for('download', run_id=run_id, filename=paths['bip_png']|basename) }}"
           data-full="{{ url_for('download', run_id=run_id, filename=paths['bip_png_full']|basename) }}"
           alt="Bipartite graph" />
    </div>

    <div id="projected_graph" class="panel tab-content" style="display:none">
      <div class="toggle">
        <label><input type="checkbox" id="proj_full_toggle"> Show full labels</label>
        <div style="color:#66788f;font-size:13px">Toggle to show full addresses / txids on the graph (may be crowded).</div>
      </div>
      <img id="proj_img" class="graph" src="{{ url_for('download', run_id=run_id, filename=paths['proj_png']|basename) }}"
           data-short="{{ url_for('download', run_id=run_id, filename=paths['proj_png']|basename) }}"
           data-full="{{ url_for('download', run_id=run_id, filename=paths['proj_png_full']|basename) }}"
           alt="Projected graph" />
    </div>

    <div id="bip_table" class="panel tab-content" style="display:none">
      <h3>Bipartite edges (preview)</h3>
      <div class="table-wrap">
        {% if csvs.bip.columns %}
          <table class="csv-table"><thead><tr>{% for c in csvs.bip.columns %}<th>{{ c }}</th>{% endfor %}</tr></thead>
          <tbody>{% for row in csvs.bip.rows %}<tr>{% for c in csvs.bip.columns %}<td>{{ row[c] }}</td>{% endfor %}</tr>{% endfor %}</tbody></table>
        {% else %}<p>No data</p>{% endif %}
      </div>
    </div>

    <div id="proj_table" class="panel tab-content" style="display:none">
      <h3>Projected address→address (preview)</h3>
      <div class="table-wrap">
        {% if csvs.proj.columns %}
          <table class="csv-table"><thead><tr>{% for c in csvs.proj.columns %}<th>{{ c }}</th>{% endfor %}</tr></thead>
          <tbody>{% for row in csvs.proj.rows %}<tr>{% for c in csvs.proj.columns %}<td>{{ row[c] }}</td>{% endfor %}</tr>{% endfor %}</tbody></table>
        {% else %}<p>No data</p>{% endif %}
      </div>
    </div>

    <div id="clusters_table" class="panel tab-content" style="display:none">
      <h3>Clusters (preview)</h3>
      <div class="table-wrap">
        {% if csvs.clusters.columns %}
          <table class="csv-table"><thead><tr>{% for c in csvs.clusters.columns %}<th>{{ c }}</th>{% endfor %}</tr></thead>
          <tbody>{% for row in csvs.clusters.rows %}<tr>{% for c in csvs.clusters.columns %}<td>{{ row[c] }}</td>{% endfor %}</tr>{% endfor %}</tbody></table>
        {% else %}<p>No data</p>{% endif %}
      </div>
    </div>

    <div id="flags_table" class="panel tab-content" style="display:none">
      <h3>Transaction flags (preview)</h3>
      <div class="table-wrap">
        {% if csvs.flags.columns %}
          <table class="csv-table"><thead><tr>{% for c in csvs.flags.columns %}<th>{{ c }}</th>{% endfor %}</tr></thead>
          <tbody>{% for row in csvs.flags.rows %}<tr>{% for c in csvs.flags.columns %}<td>{{ row[c] }}</td>{% endfor %}</tr>{% endfor %}</tbody></table>
        {% else %}<p>No data</p>{% endif %}
      </div>
    </div>

  </div>

<script>
  // Tab switching
  document.querySelectorAll('.tab').forEach(t => {
    t.addEventListener('click', function(){
      document.querySelectorAll('.tab').forEach(x=>x.classList.remove('active'));
      document.querySelectorAll('.tab-content').forEach(x=>x.style.display='none');
      this.classList.add('active');
      const target = this.getAttribute('data-target');
      const el = document.getElementById(target);
      if (el) el.style.display = 'block';
      window.scrollTo({ top: 0, behavior: 'smooth' });
    });
  });

  // Graph label toggles
  const bipToggle = document.getElementById('bip_full_toggle');
  const bipImg = document.getElementById('bip_img');
  if (bipToggle) {
    bipToggle.addEventListener('change', function(){
      bipImg.src = this.checked ? bipImg.getAttribute('data-full') : bipImg.getAttribute('data-short');
    });
  }
  const projToggle = document.getElementById('proj_full_toggle');
  const projImg = document.getElementById('proj_img');
  if (projToggle) {
    projToggle.addEventListener('change', function(){
      projImg.src = this.checked ? projImg.getAttribute('data-full') : projImg.getAttribute('data-short');
    });
  }
</script>
</body>
</html>
"""

### -------------------- Routes --------------------
@app.route("/")
def index():
    return render_template_string(INDEX_HTML)

@app.route("/analyze", methods=["POST"])
def analyze():
    txs=[]; label=request.form.get("label",""); text=request.form.get("txids","")
    if text: txs.extend([l.strip() for l in text.splitlines() if l.strip()])
    f=request.files.get("txfile")
    if f:
        content=f.read().decode(errors="ignore")
        txs.extend([l.strip() for l in content.splitlines() if l.strip()])
    if not txs:
        flash("No txids provided","error"); return redirect(url_for("index"))
    run_id=uuid.uuid4().hex[:12]; outdir=os.path.join(OUTPUT_ROOT,run_id)
    try:
        paths=process_txids(txs,outdir)
    except Exception as e:
        if os.path.isdir(outdir): shutil.rmtree(outdir, ignore_errors=True)
        flash(f"Processing failed: {e}","error"); return redirect(url_for("index"))
    csvs={"bip":read_csv_preview(paths['bip'], max_rows=500),"proj":read_csv_preview(paths['proj'],max_rows=500),"clusters":read_csv_preview(paths['clusters'],max_rows=500),"flags":read_csv_preview(paths['flags'],max_rows=500)}
    return render_template_string(RESULT_HTML, run_id=run_id, paths=paths, label=label, csvs=csvs)

@app.route("/download/<run_id>/<filename>")
def download(run_id, filename):
    outdir=os.path.join(OUTPUT_ROOT, run_id)
    if not os.path.isdir(outdir): return "Run not found", 404
    return send_from_directory(outdir, filename, as_attachment=False)


# -------------------- Peel Chain Analysis: new route & helpers --------------------

# -------------------- Peel Chain Analysis: new route & helpers (REPLACE existing peel section) --------------------

from math import isfinite
import io

def compute_peel_score(peel_chain):
    """
    Simpler, more conservative peel-score inspired by literature (monotonic remainder hops,
    stable ratios, presence of small peel outputs, and number of hops).
    Returns (score_float, details_dict).

    Notes:
      - monotonicity: fraction of consecutive hops where value does not increase
      - ratio_stability: closeness of hop ratios to a target (0.8 nominal)
      - small_peel_presence: fraction of hops in which the spending tx had at least one
        small output that looks like a "peel" relative to the continuation
      - hop_count_factor: small boost if there are multiple hops (peels typically have >1 hop)
    """
    vals = []
    sources = []
    for hop in peel_chain:
        v = hop.get("value_sats")
        if isinstance(v, (int, float)) and v > 0:
            vals.append(float(v))
            sources.append(hop.get("value_source", "unknown"))

    details = {
        "n_hops_total": len(peel_chain),
        "n_numeric_hops": len(vals),
        "value_sources": sources
    }

    # Not enough numeric hops => very low confidence
    if len(vals) < 2:
        return 0.0, {**details, "reason": "not_enough_numeric_hops"}

    # 1) Monotonicity (0..1)
    monotonic_count = sum(1 for a, b in zip(vals, vals[1:]) if b <= a + 1e-9)
    monotonicity = monotonic_count / (len(vals) - 1)

    # 2) Ratio stability (0..1)
    ratios = [vals[i+1] / vals[i] for i in range(len(vals)-1) if vals[i] > 0]
    if ratios:
        mean_ratio = sum(ratios) / len(ratios)
        var = sum((r - mean_ratio)**2 for r in ratios) / len(ratios)
        sd = var**0.5
        # prefer mean_ratio around 0.6-0.95, nominal 0.8
        mean_score = 1.0 - min(1.0, abs(mean_ratio - 0.8) / 0.8)
        sd_score = 1.0 / (1.0 + sd * 8.0)  # penalize high dispersion
        ratio_stability = (mean_score * 0.7 + sd_score * 0.3)
        ratio_stability = max(0.0, min(1.0, ratio_stability))
    else:
        ratio_stability = 0.0

    # 3) Small-peel presence: check spent tx outputs for small outputs relative to continuation
    small_peel_hits = 0
    checks = 0
    SMALL_PEEL_REL = 0.05   # define "small" as <= 5% of continuation (configurable)
    for hop in peel_chain:
        spent_txid = hop.get("spent_in_tx")
        cont_val = hop.get("value_sats") or 0
        if not spent_txid or cont_val <= 0:
            continue
        checks += 1
        try:
            stx = get_tx_json(spent_txid)
            outs = [o.get("value", 0) for o in stx.get("vout", []) if isinstance(o.get("value", 0), (int, float))]
            if not outs:
                continue
            # exclude largest output (likely continuation) and see if there's at least one small output
            largest = max(outs)
            other_outs = [o for o in outs if o != largest]
            # If there's any out <= SMALL_PEEL_REL * cont_val, count as a peel-like small payment
            found_small = any((o <= max(1, cont_val * SMALL_PEEL_REL)) for o in other_outs)
            if found_small:
                small_peel_hits += 1
        except Exception:
            # ignore fetch failures; don't bias toward peel
            continue

    small_peel_presence = (small_peel_hits / checks) if checks > 0 else 0.0

    # 4) Hop count factor (encourage at least a few hops)
    hop_count = len(vals)
    hop_factor = min(1.0, hop_count / 6.0)   # saturates at 6 hops

    # Combine with conservative weights
    w_mon = 0.40
    w_ratio = 0.30
    w_small = 0.20
    w_hop = 0.10
    score = (w_mon * monotonicity +
             w_ratio * ratio_stability +
             w_small * small_peel_presence +
             w_hop * hop_factor)
    score = max(0.0, min(1.0, score))

    details.update({
        "monotonicity": round(monotonicity, 3),
        "ratio_stability": round(ratio_stability, 3),
        "small_peel_presence": round(small_peel_presence, 3),
        "hop_factor": round(hop_factor, 3),
        "raw_ratios": [round(r, 4) for r in ratios],
        "weights": {"monotonic": w_mon, "ratio": w_ratio, "small": w_small, "hop": w_hop}
    })
    return score, details

def trace_peel_chain(txid, vout_index=0, max_hops=8, force_vout=False):
    """
    Follow a specific vout (txid:vout_index) forward through spends up to max_hops.
    Tries to extract the vout value from outspends; if absent (or force_vout=True),
    falls back to fetching the source tx.vout to get the value. If that fails, attempts
    to proxy value from the spent tx largest output (last-resort).
    Returns list of hop dicts with keys:
      - from_tx, from_vout, value_sats (int or 0), value_source, spent (bool),
        spent_in_tx, spent_in_vin_index, spent_addr, raw_outspend (optional)
    """
    chain = []
    cur_tx = txid
    cur_vout = vout_index

    for hop in range(max_hops):
        try:
            outspends = get_outspends(cur_tx)
        except Exception as e:
            chain.append({"from_tx": cur_tx, "from_vout": cur_vout, "value_sats": None, "value_source": "outspends_error", "error": f"outspends_failed:{e}"})
            break

        if cur_vout >= len(outspends):
            chain.append({"from_tx": cur_tx, "from_vout": cur_vout, "value_sats": None, "value_source": "vout_index_out_of_range", "error": "vout_index_out_of_range"})
            break

        out = outspends[cur_vout]
        # store raw outspend for debugging transparency
        hop_record = {"from_tx": cur_tx, "from_vout": cur_vout, "raw_outspend": out}

        # try value from outspends
        value = out.get("value") if isinstance(out.get("value"), (int, float)) else None
        value_source = None
        if force_vout or not value:
            # fallback: fetch tx.vout
            try:
                tx_json = get_tx_json(cur_tx)
                vouts = tx_json.get("vout", [])
                if cur_vout < len(vouts):
                    value = vouts[cur_vout].get("value") or 0
                    value_source = "tx_vout"
                else:
                    value_source = "tx_vout_missing_index"
            except Exception:
                value_source = "tx_vout_error"

        if not value and not value_source:
            # try using outspends value if present but maybe falsy
            if isinstance(out.get("value"), (int, float)) and out.get("value") > 0:
                value = out.get("value")
                value_source = "outspends"
        if not value:
            # Last-resort: try to proxy by looking at the spent tx's largest output
            spent = out.get("spent", False)
            spent_txid = out.get("txid")
            if spent and spent_txid:
                try:
                    sp_tx = get_tx_json(spent_txid)
                    outs_sp = sp_tx.get("vout", [])
                    if outs_sp:
                        proxy_val = max((o.get("value", 0) for o in outs_sp), default=0)
                        if proxy_val > 0:
                            value = proxy_val
                            value_source = "proxy_spent_largest"
                except Exception:
                    value_source = "proxy_error"
        # final fallback
        if value is None:
            value = 0
            if not value_source:
                value_source = "unknown"

        spent = out.get("spent", False)
        spent_txid = out.get("txid")
        spent_vin = out.get("vin", None)
        spent_addr = None
        if spent and spent_txid:
            try:
                sp_tx = get_tx_json(spent_txid)
                outs = sp_tx.get("vout", [])
                if outs:
                    candidate = max(outs, key=lambda o: o.get("value", 0))
                    spent_addr = candidate.get("scriptpubkey_address") or "NON_STD"
            except Exception:
                spent_addr = None

        hop_record.update({
            "value_sats": int(value) if isinstance(value, (int, float)) else 0,
            "value_source": value_source,
            "spent": spent,
            "spent_in_tx": spent_txid,
            "spent_in_vin_index": spent_vin,
            "spent_addr": spent_addr
        })
        chain.append(hop_record)

        if not spent or not spent_txid:
            break
        # move to next hop
        cur_tx = spent_txid
        cur_vout = 0
        time.sleep(SLEEP)

    return chain


PEEL_INDEX_HTML = """
<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <title>Peel Chain — UTXO Tracer</title>
  <link href="https://fonts.googleapis.com/css2?family=Inter:wght@300;400;600;700&display=swap" rel="stylesheet">
  <style>
    :root{
      --bg:#f6f8fb; --card:#ffffff; --muted:#66788f; --accent:#0b6ff2; --panel:#fbfdff;
      --border: #eef6ff; --danger: #c05621;
    }
    body { font-family: 'Inter', system-ui, -apple-system, "Segoe UI", Roboto, Helvetica, Arial; background:var(--bg); color:#0b1726; margin:0; padding:20px; }
    .container{ max-width:1100px; margin:12px auto; }
    .card{ background:var(--card); border-radius:12px; padding:18px; box-shadow:0 8px 30px rgba(16,24,40,0.06); }
    h1{ margin:0 0 8px 0; font-size:22px; }
    p.lead{ color:var(--muted); margin:6px 0 16px 0; }
    form .row{ display:flex; gap:12px; margin-bottom:10px; }
    input[type=text], input[type=number], textarea { width:100%; padding:10px; border-radius:8px; border:1px solid #e6eef8; font-size:14px; box-sizing:border-box; }
    textarea { min-height:120px; resize:vertical; }
    label.inline { display:flex; align-items:center; gap:8px; font-size:14px; color:var(--muted); }
    .controls { display:flex; gap:12px; align-items:center; margin-top:12px; }
    button.primary { background:var(--accent); color:#fff; border:none; padding:10px 14px; border-radius:8px; cursor:pointer; font-weight:600; }
    .small-muted{ color:var(--muted); font-size:13px; }
    footer { text-align:center; color:#8892a6; font-size:13px; margin-top:14px; }
    .help { font-size:13px; color:var(--muted); margin-top:8px; }
    @media (max-width:780px){
      .row { flex-direction:column; }
      .controls { flex-direction:column; align-items:stretch; }
    }
  </style>
</head>
<body>
  <div class="container">
    <div class="card">
      <h1>Peel Chain Analysis</h1>
      <p class="lead">Follow a UTXO forward to detect peel-style extraction (remainder hopping + small "peels"). Paste a starting TXID and vout index, then Analyze.</p>

      <form method="post" action="/peel" enctype="multipart/form-data">
        <div class="row">
          <div style="flex:2">
            <label><strong>Starting TXID</strong></label>
            <input type="text" name="txid" placeholder="e.g. e8b406091959700dbffcff30a60b1901337..." required />
          </div>
          <div style="width:120px">
            <label><strong>Vout</strong></label>
            <input type="number" name="vout" value="0" min="0" />
          </div>
          <div style="width:140px">
            <label><strong>Max hops</strong></label>
            <input type="number" name="max_hops" value="8" min="1" max="64" />
          </div>
        </div>

        <div style="display:flex;gap:12px;align-items:center;flex-wrap:wrap">
          <label class="inline"><input type="checkbox" name="force_vout" value="1"> Force tx.vout lookup</label>
          <label class="inline"><input type="checkbox" name="include_raw" value="1"> Include raw outspend JSON in CSV</label>
          <div style="flex:1" class="help">Force tx.vout is useful when Esplora outspends lack explicit values. Include raw output for debugging.</div>
        </div>

        <div class="controls">
          <button class="primary" type="submit">Analyze Peel Chain</button>
          <a href="/" style="text-decoration:none;color:var(--accent);font-weight:600">← Back to UTXO Tracer</a>
        </div>
      </form>

      <hr style="border:none;border-top:1px solid #f1f6fb;margin:14px 0" />
      <div class="small-muted">
        Default API: <code>{{ ESPLORA }}</code> — set <code>ESPLORA_API</code> to use a local Esplora/Esplora-like endpoint.
      </div>
    </div>

    <footer>Local tool — polite to public APIs. Outputs are written under <code>outputs/&lt;run_id&gt;</code>.</footer>
  </div>
</body>
</html>
"""

PEEL_RESULT_HTML = """
<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <title>Peel Chain — Results</title>
  <link href="https://fonts.googleapis.com/css2?family=Inter:wght@300;400;600;700&display=swap" rel="stylesheet">
  <style>
    body{ font-family:Inter,system-ui,-apple-system,Segoe UI,Roboto,Helvetica,Arial; background:#f6f8fb; color:#0b1726; padding:18px; }
    .container{ max-width:1000px; margin:12px auto; }
    .card{ background:#fff; border-radius:12px; padding:18px; box-shadow:0 8px 30px rgba(16,24,40,0.06); }
    .row{ display:flex; gap:14px; align-items:center; justify-content:space-between; }
    .score { font-size:36px; font-weight:800; color:#0b6ff2; }
    .interpret { font-size:16px; color:#374151; margin-top:6px; }
    .details { font-family:monospace; white-space:pre-wrap; background:#f8fafc; padding:12px; border-radius:8px; border:1px solid #eef6ff; margin-top:12px; font-size:13px; color:#0b1726; }
    .table-wrap{ margin-top:16px; border:1px solid #f0f4fb; padding:8px; background:#fff; border-radius:6px; overflow:auto; max-height:420px; }
    table.csv-table{ border-collapse:collapse; width:100%; font-size:13px; }
    table.csv-table th, table.csv-table td{ border:1px solid #f0f4fb; padding:8px 10px; text-align:left; vertical-align:top; }
    table.csv-table th{ position:sticky; top:0; background:#fbfdff; font-weight:700; }
    .controls{ display:flex; gap:10px; margin-top:12px; }
    a.button{ background:#0b6ff2; color:white; padding:8px 12px; border-radius:8px; text-decoration:none; font-weight:700; }
    .muted{ color:#66788f; font-size:13px; margin-top:4px; }
  </style>
</head>
<body>
  <div class="container">
    <div class="card">
      <div class="row">
        <div>
          <div style="font-weight:700;font-size:18px">Peel Chain Analysis</div>
          <div class="muted">Start: <strong>{{ label }}</strong> — Run: <strong>{{ run_id }}</strong></div>
        </div>
        <div style="text-align:right">
          <div class="score">{{ score_display }}</div>
          <div class="interpret">{{ interpretation }}</div>
        </div>
      </div>

      <div style="margin-top:14px;">
        <div style="font-weight:700">Score details</div>
        <div class="details">{{ details_text }}</div>
      </div>

      <div style="margin-top:14px;">
        <div style="font-weight:700">Chain table (preview)</div>
        <div class="table-wrap">
          {% if csv_preview.columns %}
            <table class="csv-table">
              <thead><tr>{% for c in csv_preview.columns %}<th>{{ c }}</th>{% endfor %}</tr></thead>
              <tbody>{% for row in csv_preview.rows %}<tr>{% for c in csv_preview.columns %}<td>{{ row[c] }}</td>{% endfor %}</tr>{% endfor %}</tbody>
            </table>
          {% else %}
            <div style="padding:18px;color:#66788f">No chain data available.</div>
          {% endif %}
        </div>
      </div>

      <div class="controls">
        <a class="button" href="{{ url_for('download', run_id=run_id, filename=csv_name) }}">Download CSV</a>
        <a class="button" href="{{ url_for('peel') }}" style="background:#6b7280">New analysis</a>
        <a class="button" href="{{ url_for('index') }}" style="background:#e6eef8;color:#0b6ff2">Back to tracer</a>
      </div>
    </div>
  </div>
</body>
</html>
"""


@app.route("/peel", methods=["GET", "POST"])
def peel():
    if request.method == "GET":
        return render_template_string(PEEL_INDEX_HTML)
    # POST -> run analysis
    start_tx = request.form.get("txid", "").strip()
    if not start_tx:
        flash("Please provide a txid", "error")
        return redirect(url_for("peel"))
    try:
        vout_index = int(request.form.get("vout", "0"))
    except Exception:
        vout_index = 0
    try:
        max_hops = int(request.form.get("max_hops", "8"))
    except Exception:
        max_hops = 8

    force_vout = bool(request.form.get("force_vout"))
    include_raw = bool(request.form.get("include_raw"))

    run_id = uuid.uuid4().hex[:12]
    outdir = os.path.join(OUTPUT_ROOT, run_id)
    os.makedirs(outdir, exist_ok=True)

    chain = trace_peel_chain(start_tx, vout_index=vout_index, max_hops=max_hops, force_vout=force_vout)

    # Write CSV (include value_source and optionally raw outspend json)
    csv_name = "peel_chain.csv"
    csv_path = os.path.join(outdir, csv_name)
    rows = []
    for hop in chain:
        row = {
            "from_tx": hop.get("from_tx"),
            "from_vout": hop.get("from_vout"),
            "value_sats": hop.get("value_sats"),
            "value_btc": sats_to_btc(hop.get("value_sats") or 0),
            "value_source": hop.get("value_source"),
            "spent": hop.get("spent"),
            "spent_in_tx": hop.get("spent_in_tx"),
            "spent_addr": hop.get("spent_addr"),
            "spent_in_vin_index": hop.get("spent_in_vin_index"),
            "error": hop.get("error", "")
        }
        if include_raw:
            row["raw_outspend"] = str(hop.get("raw_outspend", ""))
        rows.append(row)

    # write csv
    if rows:
        fieldnames = list(rows[0].keys())
    else:
        fieldnames = ["from_tx","from_vout","value_sats","value_btc","value_source","spent","spent_in_tx","spent_addr","spent_in_vin_index","error"]
    with open(csv_path, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=fieldnames)
        w.writeheader()
        for r in rows:
            w.writerow(r)

    # Compute score and interpretation
    score, details = compute_peel_score(chain)
    if score >= 0.7:
        interpretation = "Likely peel chain"
    elif score >= 0.4:
        interpretation = "Possible peel chain"
    else:
        interpretation = "No clear peel chain"

    # --- simplified: no image rendering, return score + CSV preview ---
    csv_preview = read_csv_preview(csv_path, max_rows=500)
    details_text = "\n".join([f"{k}: {v}" for k, v in details.items()])

    # interpretation threshold (adjustable)
    if score >= 0.75:
        interpretation = "Likely peel chain"
    elif score >= 0.45:
        interpretation = "Possible peel chain"
    else:
        interpretation = "No clear peel chain"

    # Render a simplified results page (no heavy visualization)
    return render_template_string(PEEL_RESULT_HTML,
                                  run_id=run_id,
                                  label=f"peel:{start_tx}:{vout_index}",
                                  score_display=round(score, 3),
                                  interpretation=interpretation,
                                  details_text=details_text,
                                  csv_preview=csv_preview,
                                  csv_name=csv_name,
                                  img_name=None)


# Note: existing /download/<run_id>/<filename> route will serve the peel outputs.


# -------------------- Clustering page / helpers --------------------
CLUSTERS_INDEX_HTML = """
<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <title>Address clustering — UTXO Tracer</title>
  <link href="https://fonts.googleapis.com/css2?family=Inter:wght@300;400;600;700&display=swap" rel="stylesheet">
  <style>
    body { font-family:'Inter',system-ui,-apple-system,Segoe UI,Roboto,Helvetica,Arial; padding:18px; background:#f6f8fb; color:#111; }
    .card { background:#fff; border-radius:10px; padding:18px; box-shadow:0 6px 18px rgba(16,24,40,0.06); max-width:1100px; margin:10px auto; }
    input[type=text], input[type=number] { width:100%; padding:10px; border:1px solid #e6eef8; border-radius:8px; }
    button { background:#0b6ff2; color:#fff; border:none; padding:10px 14px; border-radius:8px; cursor:pointer; font-weight:600; }
    .muted{ color:#66788f; font-size:13px; }
    footer { text-align:center; color:#8892a6; font-size:13px; margin-top:14px; }
  </style>
</head>
<body>
  <div class="card">
    <h2>Address clustering</h2>
    <p class="muted">Enter a Bitcoin address and the app will attempt to find likely change addresses / cluster membership using several heuristics.</p>

    <form method="post" action="/clusters">
      <label><strong>Address</strong></label>
      <input type="text" name="address" placeholder="bc1q..." required />
      <div style="display:flex;gap:12px;margin-top:12px;">
        <div style="width:200px">
          <label><strong>Max txs</strong></label>
          <input type="number" name="max_txs" value="200" min="10" max="2000"/>
        </div>
        <div style="width:220px">
          <label><strong>Only confirmed</strong></label>
          <select name="confirmed_only" style="padding:8px;border-radius:8px;border:1px solid #e6eef8">
            <option value="1" selected>Yes</option><option value="0">No (include mempool)</option>
          </select>
        </div>
      </div>

      <div style="margin-top:12px">
        <button type="submit">Find clusters</button>
        <a href="/" style="margin-left:12px;color:#0b6ff2;font-weight:700;text-decoration:none">← Back</a>
      </div>
    </form>
  </div>
  <footer>Uses Esplora API: <code>{{ ESPLORA }}</code></footer>
</body>
</html>
"""

CLUSTERS_RESULT_HTML = """
<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <title>Clustering results — UTXO Tracer</title>
  <link href="https://fonts.googleapis.com/css2?family=Inter:wght@300;400;600;700&display=swap" rel="stylesheet">
  <style>
    body{font-family:Inter,system-ui,-apple-system,Segoe UI,Roboto,Helvetica,Arial;background:#f6f8fb;padding:18px;color:#0b1726}
    .container{max-width:1200px;margin:10px auto}
    .card{background:#fff;border-radius:12px;padding:16px;box-shadow:0 8px 30px rgba(16,24,40,0.06)}
    .row{display:flex;justify-content:space-between;align-items:center}
    .muted{color:#66788f;font-size:13px}
    .table-wrap{margin-top:12px;border:1px solid #f0f4fb;padding:8px;background:#fff;border-radius:6px;overflow:auto;max-height:420px}
    table.csv-table{border-collapse:collapse;width:100%;font-size:13px}
    table.csv-table th, table.csv-table td{border:1px solid #f0f4fb;padding:8px 10px;text-align:left;vertical-align:top}
    table.csv-table th{position:sticky;top:0;background:#fbfdff;font-weight:700}
    .controls{display:flex;gap:10px;margin-top:12px}
    a.button{background:#0b6ff2;color:white;padding:8px 12px;border-radius:8px;text-decoration:none;font-weight:700}
    pre.details{background:#f8fafc;border:1px solid #eef6ff;padding:12px;border-radius:8px;white-space:pre-wrap}
  </style>
</head>
<body>
  <div class="container">
    <div class="card">
      <div class="row">
        <div>
          <div style="font-weight:700">Clustering results for <strong>{{ address }}</strong></div>
          <div class="muted">Scanned up to {{ txs_scanned }} txs — found {{ clusters_count }} cluster members (including seed)</div>
        </div>
        <div>
          <a class="button" href="{{ url_for('clusters') }}">New search</a>
        </div>
      </div>

      <div style="margin-top:12px">
        <div style="font-weight:700">Heuristic summary</div>
        <pre class="details">{{ summary_text }}</pre>
      </div>

      <div style="margin-top:12px">
        <div style="font-weight:700">Cluster members (preview)</div>
        <div class="table-wrap">
          {% if csv_preview.columns %}
            <table class="csv-table">
              <thead><tr>{% for c in csv_preview.columns %}<th>{{ c }}</th>{% endfor %}</tr></thead>
              <tbody>{% for row in csv_preview.rows %}<tr>{% for c in csv_preview.columns %}<td>{{ row[c] }}</td>{% endfor %}</tr>{% endfor %}</tbody>
            </table>
          {% else %}
            <div style="padding:18px;color:#66788f">No results.</div>
          {% endif %}
        </div>
      </div>

      <div class="controls">
        <a class="button" href="{{ url_for('download', run_id=run_id, filename=csv_name) }}">Download CSV</a>
        <a class="button" href="{{ url_for('index') }}" style="background:#e6eef8;color:#0b6ff2">Back to tracer</a>
      </div>
    </div>
  </div>
</body>
</html>
"""

# helper: fetch address txs (paginated)
def get_address_txs(addr, limit=500, confirmed_only=True):
    """
    Fetch txs for address using Esplora /address/:addr/txs (paginated).
    Returns a list of tx JSON objects (most recent first). We stop when limit reached.
    If confirmed_only=True we filter out mempool txs by relying on Esplora's result (it returns mempool txs too).
    """
    out = []
    url_base = f"{ESPLORA}/address/{addr}/txs"
    last_seen = None
    try:
        while len(out) < limit:
            url = url_base if not last_seen else f"{url_base}/chain/{last_seen}"
            r = requests.get(url, timeout=30)
            r.raise_for_status()
            page = r.json()
            if not page:
                break
            for tx in page:
                # Esplora uses status.confirmed (boolean) rather than a numeric confirmations field.
                if confirmed_only and not tx.get("status", {}).get("confirmed", False):
                    # skip mempool/unconfirmed txs
                    continue
                out.append(tx)
                if len(out) >= limit:
                    break
            # Esplora's /txs returns a page; last element's txid used as chain param
            last_seen = page[-1].get("txid")
            # if page length < 25 (default) we are at end
            if len(page) < 25:
                break
    except Exception:
        # best-effort: return whatever we collected
        pass
    return out

def is_address_single_use_in_tx(addr, txid):
    """
    Heuristic: address appears only in this transaction (no prior txs).
    Uses cached address txs to avoid repeated API calls during a run.
    """
    try:
        txs = _cached_address_txs(addr)
        if not txs:
            return False
        # If only 1 tx and it matches txid -> address seen only here
        if len(txs) == 1 and txs[0].get("txid") == txid:
            return True
        # If first tx is the txid and the list length >=1, we can also consider "first seen"
        # but keep conservative: require single-occurrence in this run
        return False
    except Exception:
        return False


def trailing_zeros_in_sats(sats):
    """Return number of trailing zeros in integer sats (fast heuristic for 'round' amounts)."""
    if not isinstance(sats, int) or sats <= 0:
        return 0
    tz = 0
    while sats % 10 == 0:
        tz += 1
        sats //= 10
    return tz

# add near other helpers
def get_address_stats(addr):
    """Return Esplora address info (chain_stats) or None on failure."""
    if not addr or addr.startswith("NON_STD") or addr.startswith("UNKNOWN"):
        return None
    url = f"{ESPLORA}/address/{addr}"
    try:
        r = requests.get(url, timeout=12)
        r.raise_for_status()
        return r.json().get("chain_stats", {})  # contains tx_count, funded_txo_sum, etc.
    except Exception:
        return None

def detect_change_candidates_for_tx(tx_json, target_input_addrs=None):
    """
    For a given tx_json, compute candidate change outputs and score each candidate with heuristics:
     - decimal/rounding: trailing zeros & decimal length heuristic
     - script_type: matches majority input script type
     - not equal-output (coinjoin detection): penalize if equal outputs
    NOTE: uniqueness / 'first-seen only' check has been removed to make this lighter and
    to produce possible candidates for manual review.
    Returns list of dicts: {"vout_index", "address","value_sats","score", "flags":[...]}
    """
    results = []
    vouts = tx_json.get("vout", [])
    txid = tx_json.get("txid")
    # coinjoin check: if two or more outputs share same value -> coinjoin-like
    values = [int(v.get("value") or 0) for v in vouts]
    value_counts = {}
    for v in values:
        value_counts[v] = value_counts.get(v, 0) + 1
    is_coinjoin_like = any(cnt >= 2 for cnt in value_counts.values())

    # majority input script type
    vin_script_types = []
    for vin in tx_json.get("vin", []):
        prev = vin.get("prevout") or {}
        st = prev.get("scriptpubkey_type")
        if st:
            vin_script_types.append(st)
    major_script = None
    if vin_script_types:
        major_script = max(set(vin_script_types), key=vin_script_types.count)

    total_in = sum([int(vin.get("prevout", {}).get("value", 0) or 0) for vin in tx_json.get("vin", [])]) or 1

    for idx, v in enumerate(vouts):
        addr = v.get("scriptpubkey_address") or f"NON_STD_{idx}"
        sats = int(v.get("value") or 0)
        flags = []
        score = 0.0

        # Decimal / rounding heuristic
        try:
            btc_str = f"{sats/1e8:.8f}"
            dec_part = btc_str.split(".")[1].rstrip("0")
            dec_len = len(dec_part)
            if dec_len >= 6:   # many decimals -> likely random/change-like
                flags.append("high_decimal")
                score += 0.20
        except Exception:
            pass

        # trailing zeros heuristic: if sats has many trailing zeros -> likely round payment (not change)
        tz = trailing_zeros_in_sats(sats)
        if tz >= 5:
            flags.append("round_amount")
            score -= 0.15

        # script-type heuristic
        if major_script:
            out_script = v.get("scriptpubkey_type")
            if out_script == major_script:
                flags.append("script_match")
                score += 0.15

        # continuity heuristic: vout < total_in (remainder behavior)
        if total_in > 0 and 0 < sats < total_in * 0.95:
            flags.append("smaller_than_inputs")
            score += 0.10

        # coinjoin: if tx looks coinjoin-like, reduce confidence in any single-change candidate
        if is_coinjoin_like:
            flags.append("coinjoin_like")
            score -= 0.20

        # sanity clamp and collect
        score = max(-1.0, min(1.0, score))
        results.append({
            "vout_index": idx,
            "address": addr,
            "value_sats": sats,
            "score": round(score, 3),
            "flags": flags
        })

    # If only one output has positive score and others are negative/low, bump it (exclusive candidate)
    positive = [r for r in results if r["score"] > 0]
    if len(positive) == 1:
        positive[0]["score"] = min(1.0, positive[0]["score"] + 0.12)
        positive[0]["flags"].append("sole_positive_boost")

    return results

def cluster_from_address(seed_address, max_txs=200, confirmed_only=True):
    """
    Main driver:
      - fetch address txs
      - for every tx, apply common-input clustering (union inputs)
      - for txs spending from seed (i.e., seed used in inputs), detect change candidates and
        add likely change addresses to cluster set (via threshold)
    Returns dict with metadata, clusters list, candidates list, csv_path, and summary.
    """
    run_id = uuid.uuid4().hex[:12]
    outdir = os.path.join(OUTPUT_ROOT, "clusters_" + run_id)
    os.makedirs(outdir, exist_ok=True)

    txs = get_address_txs(seed_address, limit=max_txs, confirmed_only=confirmed_only)
    txs_scanned = len(txs)
    uf = UnionFind()
    change_candidates_all = []
    seed_in_input_txids = set()

    # 1) common-input-ownership clustering: union all input addresses within each tx
    for tx in txs:
        txid = tx.get("txid")
        inputs = []
        for vin in tx.get("vin", []):
            prev = vin.get("prevout") or {}
            addr = prev.get("scriptpubkey_address")
            if addr:
                inputs.append(addr)
        if len(inputs) >= 2:
            base = inputs[0]
            for o in inputs[1:]:
                uf.union(base, o)
        # record txids where seed address is an input (we'll inspect outputs for change)
        for vin in tx.get("vin", []):
            prev = vin.get("prevout") or {}
            if prev.get("scriptpubkey_address") == seed_address:
                seed_in_input_txids.add(txid)

    # 2) For txs where seed was an input, evaluate change candidates
    for tx in txs:
        txid = tx.get("txid")
        if txid not in seed_in_input_txids:
            continue
        candidates = detect_change_candidates_for_tx(tx, target_input_addrs=[seed_address])
        # threshold: anything with score >= 0.15 considered a *possible* change (conservative)
        for c in candidates:
            if c["score"] >= 0.15:
                # record as possible candidate (but DO NOT auto-union)
                change_candidates_all.append({**c, "source_tx": txid})
                # DO NOT call uf.union(seed_address, c["address"])  # <-- removed: not confirmed


    clusters = uf.groups()
    root = uf.find(seed_address)
    members = clusters.get(root, [])
    # ensure seed present
    if seed_address not in members:
        members.append(seed_address)

        # write CSV of members + possible candidates
    csv_name = "clusters_from_address.csv"
    csv_path = os.path.join(outdir, csv_name)
    rows = []

    # candidate aggregation: how many times each address was flagged as a possible change
    cand_map = {}
    for c in change_candidates_all:
        cand_map.setdefault(c["address"], []).append(c)

    # members from canonical UNION (common-input clusters)
    members = clusters.get(uf.find(seed_address), [])
    if seed_address not in members:
        members.append(seed_address)

    # write CSV of members + possible candidates
    csv_name = "clusters_from_address.csv"
    csv_path = os.path.join(outdir, csv_name)
    rows = []

    # candidate aggregation: how many times each address was flagged as a possible change
    cand_map = {}
    for c in change_candidates_all:
        cand_map.setdefault(c["address"], []).append(c)

    # produce rows: include both confirmed-members (from union) and possible candidates (flagged)
    seen_addresses = set()
    for m in members:
        row = {
            "address": m,
            "inferred_change_count": len(cand_map.get(m, [])),
            "possible_change": "yes" if m in cand_map else "no"
        }
        if m in cand_map:
            allflags = []
            for c in cand_map[m]:
                allflags += c.get("flags", [])
            row["flags"] = ",".join(sorted(set(allflags)))
        else:
            row["flags"] = ""
        rows.append(row)
        seen_addresses.add(m)

    # Also list any possible candidates that were NOT in the union-member list
    for addr, candlist in cand_map.items():
        if addr in seen_addresses:
            continue
        allflags = []
        for c in candlist:
            allflags += c.get("flags", [])
        rows.append({
            "address": addr,
            "inferred_change_count": len(candlist),
            "possible_change": "yes",
            "flags": ",".join(sorted(set(allflags)))
        })

    # write csv (include possible_change in header)
    with open(csv_path, "w", newline="", encoding="utf-8") as fh:
        fieldnames = ["address", "inferred_change_count", "possible_change", "flags"]
        w = csv.DictWriter(fh, fieldnames=fieldnames)
        w.writeheader()
        for r in rows:
            w.writerow(r)

    # build summary text
    summary_lines = [
        f"Seed address: {seed_address}",
        f"TXs scanned: {txs_scanned} (confirmed_only={confirmed_only})",
        f"Change candidates found: {len(change_candidates_all)}",
        f"Cluster members (count): {len(members)}",
        "",
        "Note: uniqueness (first-seen) rule is NOT being enforced in this run.",
        "These addresses are *possible* change addresses derived from heuristics (decimal/script/rounding/etc.)",
        "They are NOT confirmed cluster members — please manually verify first-seen, history and other signals.",
        "",
        "Top change candidate examples:"
    ]

    for c in change_candidates_all[:8]:
        summary_lines.append(f"- {c['address']} (tx={c['source_tx']}) sats={c['value_sats']} score={c['score']} flags={','.join(c['flags'])}")

    summary_text = "\n".join(summary_lines)

    return {
        "run_id": run_id,
        "outdir": outdir,
        "csv_path": csv_path,
        "csv_name": csv_name,
        "members": members,
        "candidates": change_candidates_all,
        "txs_scanned": txs_scanned,
        "summary_text": summary_text
    }

# routes
@app.route("/clusters", methods=["GET", "POST"])
def clusters():
    if request.method == "GET":
        return render_template_string(CLUSTERS_INDEX_HTML, ESPLORA=ESPLORA)
    addr = request.form.get("address", "").strip()
    if not addr:
        flash("Please provide an address", "error")
        return redirect(url_for("clusters"))
    try:
        max_txs = int(request.form.get("max_txs", "200"))
    except Exception:
        max_txs = 200
    confirmed_only = request.form.get("confirmed_only", "1") == "1"

    try:
        res = cluster_from_address(addr, max_txs=max_txs, confirmed_only=confirmed_only)
    except Exception as e:
        flash(f"Clustering failed: {e}", "error")
        return redirect(url_for("clusters"))

    csv_preview = read_csv_preview(res["csv_path"], max_rows=500)
    return render_template_string(CLUSTERS_RESULT_HTML,
                                  run_id=res["run_id"],
                                  outdir=res["outdir"],
                                  address=addr,
                                  txs_scanned=res["txs_scanned"],
                                  clusters_count=len(res["members"]),
                                  summary_text=res["summary_text"],
                                  csv_preview=csv_preview,
                                  csv_name=res["csv_name"])


if __name__ == "__main__":
    app.run(debug=True)
