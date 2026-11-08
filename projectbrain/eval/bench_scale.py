"""Scale & Latency Benchmark Harness (`eval/bench_scale.py`) for ProjectBrain v1.0.

Measures:
  1. Cold index time on a multi-hundred-file polyglot repository (Python, TS, Go, Rust, Java).
  2. Single-file body-only Salsa Early Cutoff incremental update latency (< 25 ms target).
  3. 3-hop `WITH RECURSIVE` SQLite CTE graph traversal latency (< 5 ms target).
  4. FTS5 sub-token BM25 search latency (< 5 ms target).
  5. Stacked In-Memory Delta Overlay (`DELTA_DB`) creation & query latency (< 10 ms target).
"""
from __future__ import annotations

import json
import tempfile
import time
from pathlib import Path

from projectbrain.brain import ProjectBrain


def run_scale_benchmark(num_files: int = 200) -> dict:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        src = root / "src"
        src.mkdir()

        # Generate a polyglot synthetic repo (Python, TS, Go, Rust, Java) with cross-file call chains
        for i in range(num_files):
            mod_kind = i % 5
            if mod_kind == 0:
                nxt = f"mod_{(i + 5) % num_files}"
                (src / f"mod_{i}.py").write_text(
                    f"from {nxt} import step_{(i + 5) % num_files}\n\n"
                    f"def step_{i}(user_id: str, amount: int = {i}) -> int:\n"
                    f"    \"\"\"Execute payment step {i} for billing pipeline.\"\"\"\n"
                    f"    return step_{(i + 5) % num_files}(user_id) + amount\n",
                    encoding="utf-8",
                )
            elif mod_kind == 1:
                (src / f"service_{i}.ts").write_text(
                    f"export class BillingNode{i} {{\n"
                    f"  processInvoice{i}(invoiceId: string): boolean {{\n"
                    f"    return invoiceId.length > {i % 7};\n"
                    f"  }}\n"
                    f"}}\n",
                    encoding="utf-8",
                )
            elif mod_kind == 2:
                (src / f"handler_{i}.go").write_text(
                    f"package main\n\n"
                    f"type Gateway{i} struct {{}}\n\n"
                    f"func (g *Gateway{i}) ServeOrder{i}(orderId string) error {{\n"
                    f"    return nil\n"
                    f"}}\n",
                    encoding="utf-8",
                )
            elif mod_kind == 3:
                (src / f"engine_{i}.rs").write_text(
                    f"pub struct LedgerEngine{i} {{}}\n\n"
                    f"impl LedgerEngine{i} {{\n"
                    f"    pub fn commit_entry_{i}(&self, entry_id: u64) -> bool {{\n"
                    f"        entry_id > {i}\n"
                    f"    }}\n"
                    f"}}\n",
                    encoding="utf-8",
                )
            else:
                (src / f"Controller{i}.java").write_text(
                    f"package com.example;\n\n"
                    f"@RequestMapping(\"/api/v1/node{i}\")\n"
                    f"public class Controller{i} {{\n"
                    f"    @PostMapping(\"/execute\")\n"
                    f"    public String executeStep{i}(String payload) {{\n"
                    f"        return payload;\n"
                    f"    }}\n"
                    f"}}\n",
                    encoding="utf-8",
                )

        brain = ProjectBrain(str(root))
        t0 = time.perf_counter()
        idx_stats = brain.index()
        cold_index_ms = (time.perf_counter() - t0) * 1000.0

        # 1. Warm no-op incremental check
        t0 = time.perf_counter()
        noop_res = brain.incremental_index()
        warm_noop_ms = (time.perf_counter() - t0) * 1000.0

        # 2. Single-file body-only edit (Salsa Early Cutoff)
        target_py = src / "mod_0.py"
        orig_py = target_py.read_text(encoding="utf-8")
        target_py.write_text(orig_py + "\n    # body comment update\n", encoding="utf-8")
        t0 = time.perf_counter()
        salsa_res = brain.incremental_index()
        salsa_cutoff_ms = (time.perf_counter() - t0) * 1000.0

        # 3. 3-hop WITH RECURSIVE SQLite CTE query
        seed = "src/mod_0.py::step_0"
        t0 = time.perf_counter()
        cte_rows = brain._db_store.query_reachable_cte([seed], max_hops=3, direction="forward")
        cte_3hop_ms = (time.perf_counter() - t0) * 1000.0

        # 4. FTS5 Sub-token BM25 search
        t0 = time.perf_counter()
        fts_hits = brain._db_store.search_fts("processInvoice LedgerEngine executeStep", limit=15)
        fts_search_ms = (time.perf_counter() - t0) * 1000.0

        # 5. Stacked In-Memory Delta Overlay (DELTA_DB)
        t0 = time.perf_counter()
        overlay_conn = brain._db_store.open_delta_overlay({
            "src/mod_0.py": brain._index["src/mod_0.py"],
        })
        cnt = overlay_conn.execute("SELECT COUNT(*) FROM temp.unified_symbols;").fetchone()[0]
        overlay_conn.close()
        delta_overlay_ms = (time.perf_counter() - t0) * 1000.0

        return {
            "files_indexed": idx_stats.get("files", num_files),
            "cold_index_ms": round(cold_index_ms, 2),
            "warm_noop_ms": round(warm_noop_ms, 2),
            "salsa_early_cutoff_ms": round(salsa_cutoff_ms, 2),
            "salsa_early_cutoff_fired": bool(salsa_res.get("salsa_early_cutoff")),
            "cte_3hop_ms": round(cte_3hop_ms, 2),
            "cte_3hop_reachable_count": len(cte_rows),
            "fts5_search_ms": round(fts_search_ms, 2),
            "fts5_hits": len(fts_hits),
            "delta_overlay_ms": round(delta_overlay_ms, 2),
            "unified_symbols_count": cnt,
        }


if __name__ == "__main__":
    res = run_scale_benchmark(200)
    print(json.dumps(res, indent=2))
