# UTXO_Peel_Cluster_analyser

# UTXO Tracer — README (short & practical)

A short, plain-English README for the Flask-based UTXO Tracer you’ve been running.

---

## What this script does (high level)

UTXO Tracer is a small local web tool that helps you investigate Bitcoin transactions and addresses. It has three main features:

* **Transaction analysis** (`/analyze`): fetches tx JSONs from an Esplora API, builds a bipartite address↔tx graph, projects flowing value to get address→address "evidence" edges, draws graphs and writes CSVs you can inspect/download.
* **Peel-chain analysis** (`/peel`): follows a specific tx:vout forward through spends to detect “peel” behavior (remainder hopping + small outgoing payments). Produces a conservative peel score and CSV of the chain.
* **Address-based clustering (possible clusters)** (`/clusters`): scans recent txs for a seed address, applies common-input clustering (union inputs) and heuristics to flag **possible** change addresses — but **does not** auto-add heuristic-only addresses to a cluster (so they are presented for manual review).

All outputs are written under `outputs/<run_id>/` (CSVs and PNGs).

---

## Quick start / run locally

1. Install the Python dependencies:

```bash
python3 -m venv venv
source venv/bin/activate
pip install flask requests networkx matplotlib
```

2. (Optional) set environment variables:

* `ESPLORA_API` — base URL of the Esplora API (defaults to `https://blockstream.info/api`)
* `ESPLORA_SLEEP` — seconds to sleep between API calls (default `0.25`)
* `FLASK_SECRET` — Flask secret key
* `OUTPUT_ROOT` — where outputs are written (default `outputs`)

Example:

```bash
export ESPLORA_API="https://blockstream.info/api"
export ESPLORA_SLEEP="0.25"
```

3. Run the app:

```bash
python app.py
# open http://127.0.0.1:5000/
```

---

## Web UI endpoints

* `GET /` — main page (paste txids or upload file)
* `POST /analyze` — analyze pasted/uploaded txids; produces graphs & CSVs
* `GET|POST /peel` — peel-chain UI / run analysis for a tx:vout
* `GET|POST /clusters` — clustering UI; give a seed address and scan recent txs
* `GET /download/<run_id>/<filename>` — download produced CSVs/PNGs

Outputs for each run are stored in `outputs/<run_id>/` and include:

* `bipartite_edges.csv` — raw addr↔tx edges
* `evidence_address_to_address.csv` — projected value edges
* `clusters.csv` (or `clusters_from_address.csv`) — cluster/possible-change output
* `tx_flags.csv` — per-tx flags & change-candidate scoring
* graph PNGs (`bipartite_graph.png`, `projected_graph.png`)

---

## How the heuristics work (short)

* **Common-input clustering**: if multiple inputs in a tx come from different addresses, they are unioned (classic CIA heuristic).

* **Change candidate detection**: for each tx we score each vout using heuristics like:

  * script-type match vs majority of inputs
  * whether output is smaller than (largest) input(s) (continuation behavior)
  * decimal-length / many decimal digits (suggests non-round/random change)
  * trailing zeros in sats (round numbers are penalized)
  * coinjoin-like detection (equal outputs reduces confidence)
  * small boosts/penalties and a final clamp to [-1..1]

* **Clustering mode**: the code **does not** automatically union addresses based solely on the change-candidate heuristic; it records them as *possible* change addresses and leaves confirmation to you. (This reduces false positives.)

* **Peel detection**: follows a UTXO through up to `max_hops`, collects values per hop and computes a score that blends monotonicity, ratio stability, presence of small peel outputs, and hop count.

---

## Known shortcomings & caveats (important — read these)

1. **False positives/negatives**
   Heuristics are probabilistic. Many legitimate payments look like change and many changes look like payments. Treat everything as *possible* until manually verified.

2. **Esplora response quirks / pagination**

   * The `/address/:addr/txs` endpoint returns pages (25 entries). To be correct for high-activity addresses you must paginate until you’ve seen the whole history or enough txs. Currently the tool fetches pages until `limit` or end-of-page, but some helper functions use cached single-page results — be mindful of partial histories.
   * Esplora’s list endpoints expose `status.confirmed` (boolean) but **do not** include a numeric `status.confirmations`. If you code a check for `confirmations` you'll treat all txs as unconfirmed. Use `status.confirmed` (boolean) or compute confirmations from current block height and `status.block_height`.

3. **Caching and first-seen uniqueness**

   * The `is_address_single_use_in_tx` helper currently depends on `_cached_address_txs`, which may be incomplete if the address has many txs. Do not rely on that helper for high-confidence "first-seen" detection unless you fetch full history or check `chain_stats.tx_count` from `/address/:addr`.

4. **Rate limits & polite API use**

   * Public Esplora instances (like blockstream.info) can throttle you. Use sensible `ESPLORA_SLEEP` delays and/or run your own indexer for heavy work.

5. **Graphs can be messy**

   * NetworkX + matplotlib renderings will be cluttered for many nodes. The graph images are for quick visual inspection, not publication-grade diagrams.

6. **No persistence or user management**

   * Output files are stored locally per run in `outputs/`. There is no database or authentication — this is a local investigative tool.

7. **Heuristics intentionally conservative**

   * For clustering you specifically removed the “unique/first-seen” rule so the tool returns *possible* change addresses. The README warns users to manually verify — this is deliberate to avoid over-clustering.

8. **Edge-cases in Esplora / value lookups**

   * Sometimes `outspends` entries lack explicit value fields. The peel-chain code attempts fallbacks (tx.vout lookup, proxy by largest output) but these are imperfect and may reduce score accuracy.

---

## Troubleshooting (common symptoms)

* **"TXs scanned: 0"** — most likely your `confirmed_only` filter is checking a non-existent `status.confirmations`. Fix by checking `status.confirmed` boolean (see above).
* **Graphs don’t render / memory errors** — too many nodes; reduce tx list or increase machine resources.
* **Missing candidates** — increase `max_txs` for `/clusters`, or set `confirmed_only`=False to include mempool (careful).
* **Slow runs / API errors** — check network, set a longer `ESPLORA_SLEEP`, or point `ESPLORA_API` to a local Esplora instance.

---

## Suggested improvements (if you want to extend it)

* Implement robust pagination and optionally fetch full address history before applying uniqueness checks.
* Add configurable thresholds for change-candidate scoring and peel scoring.
* Store results to a small DB (sqlite) for repeated queries & long-running investigations.
* Add additional heuristics: dust patterns, address reuse history, labeling from known clusters.
* Optional: parallelize API calls with rate-limit awareness for faster scanning.

---

## License & privacy note

* This is a local investigative tool — it queries public blockchain data using an Esplora API. Do not upload or expose private keys or private wallet state.
* License: add whatever license you prefer (MIT/Apache/etc.).

---

## TL;DR

* The app collects txs from Esplora, runs heuristics to suggest change outputs and clusters, and writes CSVs + graphs for manual review.
* **Important**: heuristics are not definitive. The clustering mode intentionally presents *possible* change addresses for manual verification — it does **not** force them into clusters.
* If you see `TXs scanned: 0`, check that the code uses `status.confirmed` (boolean) instead of `status.confirmations` (numeric) in the `/address/:addr/txs` loop.

---
