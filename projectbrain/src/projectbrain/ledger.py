"""Live ROI & Token Savings Ledger for ProjectBrain.

Automatically records input tokens saved, output tokens saved, file reads avoided,
estimated API cost savings (USD), and developer wait-time saved on every
`dossier`, `compile`, `slice`, and `patch` operation.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

LEDGER_FILENAME = ".projectbrain_ledger.json"

# Standard frontier coding-agent API rates (Claude Sonnet 4 / 3.7 class)
UNCACHED_INPUT_USD_PER_M = 3.00
CACHED_INPUT_USD_PER_M = 0.30
OUTPUT_USD_PER_M = 15.00
# In multi-turn agent loops, every 1 token of raw file output read into context
# is billed once as uncached input ($3/1M) + ~8x across subsequent turns as cached read ($0.30/1M)
EFFECTIVE_INPUT_CONTEXT_USD_PER_M = UNCACHED_INPUT_USD_PER_M + (8.0 * CACHED_INPUT_USD_PER_M)  # $5.40 / 1M code tokens


class SavingsLedger:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.ledger_path = root / LEDGER_FILENAME

    def _load(self) -> dict[str, Any]:
        if self.ledger_path.exists():
            try:
                data = json.loads(self.ledger_path.read_text(encoding="utf-8"))
                if isinstance(data, dict) and "events" in data:
                    return data
            except Exception:
                pass
        return {
            "repo": str(self.root.resolve()),
            "created_at": datetime.now(timezone.utc).isoformat(),
            "events": [],
        }

    def _save(self, data: dict[str, Any]) -> None:
        try:
            self.ledger_path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        except OSError:
            pass

    def record_input_op(
        self,
        op: str,
        query: str,
        delivered_tokens: int,
        naive_full_file_tokens: int,
        files_covered: int,
    ) -> dict[str, Any]:
        saved_input = max(0, naive_full_file_tokens - delivered_tokens)
        reads_avoided = max(0, files_covered - (1 if op == "dossier" else 0))
        usd_saved = round((saved_input / 1_000_000.0) * EFFECTIVE_INPUT_CONTEXT_USD_PER_M, 4)
        seconds_saved = round(reads_avoided * 4.5, 1)

        event = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "op": op,
            "query": query[:140],
            "delivered_input_tokens": delivered_tokens,
            "naive_input_tokens": naive_full_file_tokens,
            "input_tokens_saved": saved_input,
            "delivered_output_tokens": 0,
            "naive_output_tokens": 0,
            "output_tokens_saved": 0,
            "file_reads_avoided": reads_avoided,
            "est_usd_saved": usd_saved,
            "est_seconds_saved": seconds_saved,
        }
        data = self._load()
        data["events"].append(event)
        self._save(data)
        return event

    def record_patch_op(
        self,
        files_patched: int,
        ops_applied: int,
        spec_tokens: int,
        equiv_edit_tokens: int,
        mode: str = "dry_run",
    ) -> dict[str, Any]:
        saved_output = max(0, equiv_edit_tokens - spec_tokens)
        usd_saved = round((saved_output / 1_000_000.0) * OUTPUT_USD_PER_M, 4)
        # Each avoided multi-file edit_file tool call saves ~5s of generation + roundtrip latency
        edits_avoided = max(0, files_patched - 1)
        seconds_saved = round(edits_avoided * 5.0 + (saved_output / 65.0), 1)

        event = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "op": f"patch ({mode})",
            "query": f"{ops_applied} surgical ops across {files_patched} files",
            "delivered_input_tokens": 0,
            "naive_input_tokens": 0,
            "input_tokens_saved": 0,
            "delivered_output_tokens": spec_tokens,
            "naive_output_tokens": equiv_edit_tokens,
            "output_tokens_saved": saved_output,
            "file_reads_avoided": edits_avoided,
            "est_usd_saved": usd_saved,
            "est_seconds_saved": seconds_saved,
        }
        data = self._load()
        data["events"].append(event)
        self._save(data)
        return event

    def get_stats(self) -> dict[str, Any]:
        data = self._load()
        events = data.get("events", [])
        tot_deliv_in = sum(e.get("delivered_input_tokens", 0) for e in events)
        tot_naive_in = sum(e.get("naive_input_tokens", 0) for e in events)
        tot_saved_in = sum(e.get("input_tokens_saved", 0) for e in events)

        tot_deliv_out = sum(e.get("delivered_output_tokens", 0) for e in events)
        tot_naive_out = sum(e.get("naive_output_tokens", 0) for e in events)
        tot_saved_out = sum(e.get("output_tokens_saved", 0) for e in events)

        tot_reads_avoided = sum(e.get("file_reads_avoided", 0) for e in events)
        tot_usd = round(sum(e.get("est_usd_saved", 0.0) for e in events), 4)
        tot_sec = round(sum(e.get("est_seconds_saved", 0.0) for e in events), 1)

        in_pct = round((tot_saved_in / tot_naive_in) * 100, 1) if tot_naive_in > 0 else 0.0
        out_pct = round((tot_saved_out / tot_naive_out) * 100, 1) if tot_naive_out > 0 else 0.0

        return {
            "repo": data.get("repo", str(self.root.resolve())),
            "total_operations": len(events),
            "input_tokens": {
                "delivered": tot_deliv_in,
                "naive_baseline": tot_naive_in,
                "saved": tot_saved_in,
                "savings_pct": in_pct,
            },
            "output_tokens": {
                "delivered": tot_deliv_out,
                "naive_baseline": tot_naive_out,
                "saved": tot_saved_out,
                "savings_pct": out_pct,
            },
            "tool_calls_avoided": tot_reads_avoided,
            "recent_events": events[-10:],
        }

    def reset(self) -> dict[str, Any]:
        if self.ledger_path.exists():
            try:
                self.ledger_path.unlink()
            except OSError:
                pass
        return {"status": "reset", "repo": str(self.root.resolve())}
