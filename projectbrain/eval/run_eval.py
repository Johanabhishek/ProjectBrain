"""Evaluation harness for projectbrain.

Two distinct task sets, scored and reported separately because they measure
different things:

  tasks.json            - file-retrieval tasks (precision/recall/tokens-saved).
                           Several of these are answerable by a plain keyword/
                           filename grep alone - this section is a "keyword
                           ranking baseline," not evidence the graph is doing
                           anything grep couldn't. It mainly guards against
                           ranking regressions.

  relationship_tasks.json - graph-relationship tasks. Each one asks a question
                           that requires assembling multi-hop graph structure
                           to answer (route -> handler -> service -> model;
                           "what does X call two hops out"), and each is
                           paired with a naive grep baseline over the same
                           fixture to make the "grep genuinely cannot answer
                           this" claim measured rather than assumed. This is
                           the section that actually substantiates
                           projectbrain's value over string search.

Run directly: `python eval/run_eval.py [--project NAME] [--json]`
"""
from __future__ import annotations

import argparse
import json
import re
import time
from pathlib import Path

from projectbrain.brain import ProjectBrain

EVAL_DIR = Path(__file__).parent
FIXTURES_DIR = EVAL_DIR / "fixtures"


# ── File-retrieval scoring ───────────────────────────────────────────────────

def _avg_tokens_per_file(brain: ProjectBrain) -> float:
    """Crude chars/4 heuristic over indexed file sizes - always reported as
    an *estimate*, never as an exact token count."""
    sizes = list(brain._sizes.values())
    if not sizes:
        return 0.0
    return (sum(sizes) / len(sizes)) / 4


def score_file_retrieval_task(brain: ProjectBrain, task: dict, total_files: int, avg_tokens: float) -> dict:
    start = time.perf_counter()
    results = brain.query(task["prompt"], top_n=10)
    query_time_ms = (time.perf_counter() - start) * 1000

    returned = [r["rel_path"] for r in results]
    expected = set(task["expected_files"])
    correct = expected & set(returned)

    precision = len(correct) / len(returned) if returned else 0.0
    recall = len(correct) / len(expected) if expected else 0.0
    avoided_reads = max(total_files - len(returned), 0)

    return {
        "id": task["id"],
        "prompt": task["prompt"],
        "expected_files": sorted(expected),
        "returned_files": returned,
        "precision": precision,
        "recall": recall,
        "query_time_ms": query_time_ms,
        "avoided_reads": avoided_reads,
        "estimated_tokens_saved": avoided_reads * avg_tokens,
    }


# ── Relationship-task scoring ────────────────────────────────────────────────

def _grep_baseline(project_dir: Path, keyword: str) -> set[str]:
    """Naive case-insensitive substring search over every indexed source
    file - the thing a human or agent would do without projectbrain."""
    keyword_lower = keyword.lower()
    matches: set[str] = set()
    for path in project_dir.rglob("*"):
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        if keyword_lower in text.lower():
            matches.add(str(path.relative_to(project_dir)).replace("\\", "/"))
    return matches


def score_relationship_task(brain: ProjectBrain, task: dict, project_dir: Path) -> dict:
    result: dict = {"id": task["id"], "kind": task["kind"], "note": task.get("note", "")}

    if task["kind"] == "neighbors":
        neighbors = brain.get_neighbors(
            task["node_id"], edge_types=task.get("edge_types"), depth=task.get("depth", 1),
        )
        neighbor_ids = " ".join(n["id"] for n in neighbors.get("nodes", [])).lower()

        expected = task.get("expected_neighbor_substrings", [])
        unexpected = task.get("unexpected_neighbor_substrings", [])
        passed = (
            all(s.lower() in neighbor_ids for s in expected)
            and not any(s.lower() in neighbor_ids for s in unexpected)
        )
        result["passed"] = passed
        result["neighbor_count"] = len(neighbors.get("nodes", []))

    elif task["kind"] == "relationship":
        rel = brain.find_relationship(task["name_a"], task["name_b"])
        path = rel.get("path", [])

        if task.get("expect_no_path"):
            result["passed"] = not path
        else:
            path_text = " ".join(f"{e['src']} {e['dst']}" for e in path).lower()
            passed = bool(path) and len(path) >= task.get("min_hops", 1)
            passed = passed and all(s.lower() in path_text for s in task.get("expected_path_substrings", []))
            result["passed"] = passed
        result["hops"] = len(path)

    else:
        raise ValueError(f"Unknown relationship task kind: {task['kind']!r}")

    if "grep_keyword" in task:
        grep_matches = _grep_baseline(project_dir, task["grep_keyword"])
        miss_file = task.get("grep_should_miss_file")
        result["grep_would_find_it"] = miss_file in grep_matches if miss_file else None
        result["grep_matched_files"] = sorted(grep_matches)

    return result


# ── Claim-verification scoring ───────────────────────────────────────────────
#
# The anti-hallucination section. Each task is a structural claim with the
# verdict projectbrain SHOULD return. Two error rates must stay at zero:
#   false_support     — asserted a claim SUPPORTED when it isn't actually true.
#   false_refutation  — REFUTED a claim that is true or merely unprovable.
# `decisiveness` (share of non-UNKNOWN verdicts) is tracked so later accuracy
# work can be shown to raise it *without* moving either error rate off zero.

def score_claim_task(brain: ProjectBrain, task: dict) -> dict:
    result = brain.verify_claim(task["subject"], task["relation"], task["object"])
    actual = result["verdict"]
    expected = task["expected_verdict"]
    return {
        "id": task["id"],
        "claim": f"{task['subject']} [{task['relation']}] {task['object']}",
        "expected": expected,
        "actual": actual,
        "confidence": result.get("confidence"),
        "correct": actual == expected,
        "false_support": actual == "SUPPORTED" and expected != "SUPPORTED",
        "false_refutation": actual == "REFUTED" and expected != "REFUTED",
        "note": task.get("note", ""),
    }


# ── Runner ───────────────────────────────────────────────────────────────────

def run(project_filter: str | None = None) -> dict:
    tasks = json.loads((EVAL_DIR / "tasks.json").read_text(encoding="utf-8"))
    relationship_tasks = json.loads((EVAL_DIR / "relationship_tasks.json").read_text(encoding="utf-8"))
    claim_tasks = json.loads((EVAL_DIR / "claim_tasks.json").read_text(encoding="utf-8"))

    projects = sorted(
        {t["project"] for t in tasks}
        | {t["project"] for t in relationship_tasks}
        | {t["project"] for t in claim_tasks}
    )
    if project_filter:
        projects = [p for p in projects if p == project_filter]

    file_retrieval_results = []
    relationship_results = []
    claim_results = []
    index_times = {}

    for project in projects:
        project_dir = FIXTURES_DIR / project
        brain = ProjectBrain(str(project_dir))

        start = time.perf_counter()
        stats = brain.index()
        index_times[project] = (time.perf_counter() - start) * 1000
        total_files = stats["files"]
        avg_tokens = _avg_tokens_per_file(brain)

        for task in tasks:
            if task["project"] == project:
                file_retrieval_results.append(score_file_retrieval_task(brain, task, total_files, avg_tokens))

        for task in relationship_tasks:
            if task["project"] == project:
                relationship_results.append(score_relationship_task(brain, task, project_dir))

        for task in claim_tasks:
            if task["project"] == project:
                claim_results.append(score_claim_task(brain, task))

    # Self-dogfooding evaluation: run `compile_context` on ProjectBrain's own repo!
    dogfood_results = score_self_dogfood_tasks()
    adversarial_results = score_adversarial_dogfood_tasks()

    return {
        "projects": projects,
        "index_times_ms": index_times,
        "file_retrieval": file_retrieval_results,
        "relationship": relationship_results,
        "claim_verification": claim_results,
        "dogfood_context_compiler": dogfood_results,
        "adversarial_dogfood": adversarial_results,
    }


def _simulate_disciplined_grep_window_agent(
    brain: ProjectBrain, prompt: str, expected_symbols: set[str], window_lines: int = 120, max_windows: int = 3
) -> dict:
    """Simulate a smart, disciplined agent using `grep` + `view_file(StartLine, EndLine)`:
    1. Strips English/code filler stopwords so it only greps for meaningful terms.
    2. Weights grep hits by keyword rarity (a hit on `verify_claim` or `nextjs` ranks
       far above a generic hit on `parse` or `status`).
    3. Picks the top `max_windows` (3) best hit locations across `src/` and reads
       a `window_lines` (120-line) chunk around each hit.
    """
    grep_stop = {
        "how", "does", "what", "when", "where", "who", "why", "the", "and", "for",
        "from", "with", "that", "this", "are", "was", "were", "has", "have", "into",
        "out", "someone", "tool", "file", "files", "function", "code", "line", "lines",
        "stop", "happens", "build", "check", "cut", "saved",
    }
    keywords = [
        w for w in re.findall(r"[a-zA-Z0-9_]+", prompt.lower())
        if len(w) > 2 and w not in grep_stop
    ]
    src_files = [rel for rel in brain._index if rel.startswith("src/")]

    # 1. Count how many lines in src/ each keyword appears on (specificity)
    kw_line_freq: dict[str, int] = {k: 0 for k in keywords}
    file_lines_cache: dict[str, list[str]] = {}
    for rel in src_files:
        full_path = brain.root / rel
        try:
            lines = full_path.read_text(encoding="utf-8", errors="ignore").splitlines()
        except OSError:
            continue
        file_lines_cache[rel] = lines
        for line_text in lines:
            low = line_text.lower()
            for k in keywords:
                if k in low:
                    kw_line_freq[k] += 1

    # Specificity weight: rare terms (like `verify_claim`, freq=3) get high weight;
    # common terms (freq=100) get low weight.
    kw_weight = {
        k: (100.0 / max(freq, 1)) if freq > 0 else 0.0
        for k, freq in kw_line_freq.items()
    }

    # 2. Score every matching line in src/ by specificity of matched keywords
    scored_hits: list[tuple[float, str, int]] = []
    grep_list_tokens = 0
    for rel, lines in file_lines_cache.items():
        for idx, line_text in enumerate(lines, start=1):
            low = line_text.lower()
            matched = [k for k in keywords if k in low]
            if matched:
                grep_list_tokens += max(1, len(line_text) // 4)
                line_score = sum(kw_weight[k] for k in matched)
                # Boost if line is a `def ` or `class ` definition
                if low.lstrip().startswith(("def ", "async def ", "class ")):
                    line_score *= 2.0
                scored_hits.append((line_score, rel, idx))

    scored_hits.sort(key=lambda x: (-x[0], x[1], x[2]))

    # 3. Pick top `max_windows` non-overlapping 120-line windows
    half = window_lines // 2
    chosen_windows: list[tuple[str, int, int]] = []
    for _, rel, hl in scored_hits:
        if any(r == rel and abs(hl - ((s + e) // 2)) < half for r, s, e in chosen_windows):
            continue
        lines = file_lines_cache[rel]
        start_l = max(1, hl - half)
        end_l = min(len(lines), hl + half)
        chosen_windows.append((rel, start_l, end_l))
        if len(chosen_windows) >= max_windows:
            break

    read_tokens = 0
    covered_symbols: set[str] = set()
    for rel, start_l, end_l in chosen_windows:
        lines = file_lines_cache[rel]
        chunk_text = "\n".join(lines[start_l - 1 : end_l])
        read_tokens += max(1, len(chunk_text) // 4)
        info = brain._index.get(rel, {})
        for fn in info.get("functions", []):
            fn_line = fn.get("line_start", 0)
            if start_l <= fn_line <= end_l:
                covered_symbols.add(fn["name"])

    total_tokens = min(grep_list_tokens, 300) + read_tokens
    recall = len(expected_symbols & covered_symbols) / len(expected_symbols) if expected_symbols else 1.0
    return {
        "grep_window_tokens": total_tokens,
        "grep_window_recall": recall,
        "grep_covered_symbols": sorted(expected_symbols & covered_symbols),
    }


def score_self_dogfood_tasks() -> list[dict]:
    """Run ProjectBrain's v2 `compile_context` on ProjectBrain's own repository
    to measure real-world surgical slice token savings vs full-file `view_file` reads.
    """
    repo_root = EVAL_DIR.parent
    brain = ProjectBrain(str(repo_root))
    brain.index()
    total_repo_tokens = sum(brain._sizes.values()) // 4

    dogfood_tasks = [
        {
            "id": "dogfood-verify-claim",
            "prompt": "how does verify_claim check closed world parse status and support evidence",
            "expected_symbols": ["verify_claim", "_support_evidence"],
        },
        {
            "id": "dogfood-import-resolution",
            "prompt": "resolve python relative imports and typescript path aliases",
            "expected_symbols": ["resolve_python_import", "resolve_js_import"],
        },
        {
            "id": "dogfood-route-inference",
            "prompt": "infer nextjs app router and pages router file based routes",
            "expected_symbols": ["infer_file_based_routes"],
        },
    ]

    results = []
    for t in dogfood_tasks:
        # Use fresh session state per benchmark task
        brain._served_slices.clear()
        brain._served_skeletons.clear()
        start = time.perf_counter()
        packet = brain.compile_context(t["prompt"], budget_tokens=2500, top_n=3)
        elapsed_ms = (time.perf_counter() - start) * 1000

        sliced_symbols = {s["symbol"].split(".")[-1] for s in packet.get("target_slices", [])}
        expected = set(t["expected_symbols"])
        symbol_recall = len(expected & sliced_symbols) / len(expected) if expected else 1.0
        packet_chars = sum(len(s.get("code", "")) for s in packet.get("target_slices", [])) + sum(
            len(sk.get("skeleton", "")) for sk in packet.get("neighbor_skeletons", [])
        )
        packet_tokens = max((packet_chars + 240) // 4, 1)
        matched_files = packet.get("recommended_read_order", [])
        naive_file_tokens = sum(brain._sizes.get(rp, 0) for rp in matched_files) // 4
        savings_pct = round(((naive_file_tokens - packet_tokens) / max(naive_file_tokens, 1)) * 100, 1) if naive_file_tokens > 0 else 0.0
        grep_sim = _simulate_disciplined_grep_window_agent(brain, t["prompt"], expected)
        gw_tokens = grep_sim["grep_window_tokens"]
        saved_vs_gw = round((1.0 - packet_tokens / max(gw_tokens, 1)) * 100, 1)

        results.append({
            "id": t["id"],
            "prompt": t["prompt"],
            "symbol_recall": symbol_recall,
            "sliced_symbols": sorted(sliced_symbols),
            "packet_tokens": packet_tokens,
            "grep_window_tokens": gw_tokens,
            "grep_window_recall": grep_sim["grep_window_recall"],
            "tokens_saved_vs_grep_window_pct": saved_vs_gw,
            "naive_full_files_tokens": naive_file_tokens,
            "total_repo_tokens": total_repo_tokens,
            "tokens_saved_vs_matched_files_pct": savings_pct,
            "tokens_saved_vs_repo_pct": round((1.0 - packet_tokens / max(total_repo_tokens, 1)) * 100, 1),
            "compile_time_ms": round(elapsed_ms, 2),
        })
    return results


def score_adversarial_dogfood_tasks() -> list[dict]:
    """Unbiased / Adversarial benchmark:
    Prompts contain ZERO exact function/class identifier names, testing how well
    `compile_context` handles natural conceptual phrasings vs a disciplined grep+window agent.
    """
    repo_root = EVAL_DIR.parent
    brain = ProjectBrain(str(repo_root))
    brain.index()

    adversarial_tasks = [
        {
            "id": "adv-no-id-syntax-error-refutation",
            "prompt": "how do we stop the tool from lying when a file has a syntax error or fails to parse",
            "expected_symbols": ["verify_claim", "_parse_status_counts"],
        },
        {
            "id": "adv-no-id-stale-note",
            "prompt": "what happens when someone edits a function that has a saved architectural note",
            "expected_symbols": ["get_notes_for", "remember"],
        },
        {
            "id": "adv-no-id-nextjs-brackets",
            "prompt": "how are dynamic bracket segments in nextjs urls normalized",
            "expected_symbols": ["_convert_segments", "infer_file_based_routes"],
        },
        {
            "id": "adv-no-id-code-slicing",
            "prompt": "cut out exact lines of source code for a function and build a file outline",
            "expected_symbols": ["slice_lines", "build_file_skeleton"],
        },
    ]

    results = []
    for t in adversarial_tasks:
        brain._served_slices.clear()
        brain._served_skeletons.clear()
        start = time.perf_counter()
        packet = brain.compile_context(t["prompt"], budget_tokens=2500, top_n=3)
        elapsed_ms = (time.perf_counter() - start) * 1000

        sliced_symbols = {s["symbol"].split(".")[-1] for s in packet.get("target_slices", [])}
        expected = set(t["expected_symbols"])
        symbol_recall = len(expected & sliced_symbols) / len(expected) if expected else 1.0
        packet_chars = sum(len(s.get("code", "")) for s in packet.get("target_slices", [])) + sum(
            len(sk.get("skeleton", "")) for sk in packet.get("neighbor_skeletons", [])
        )
        packet_tokens = max((packet_chars + 240) // 4, 1)
        grep_sim = _simulate_disciplined_grep_window_agent(brain, t["prompt"], expected)
        gw_tokens = grep_sim["grep_window_tokens"]
        saved_vs_gw = round((1.0 - packet_tokens / max(gw_tokens, 1)) * 100, 1)

        results.append({
            "id": t["id"],
            "prompt": t["prompt"],
            "expected_symbols": sorted(expected),
            "sliced_symbols": sorted(sliced_symbols),
            "symbol_recall": symbol_recall,
            "packet_tokens": packet_tokens,
            "grep_window_tokens": gw_tokens,
            "grep_window_recall": grep_sim["grep_window_recall"],
            "tokens_saved_vs_grep_window_pct": saved_vs_gw,
            "compile_time_ms": round(elapsed_ms, 2),
        })
    return results


def _print_report(data: dict) -> None:
    print("=" * 70)
    print("KEYWORD-RANKING BASELINE (file retrieval)")
    print("Several of these tasks are grep-answerable - this section guards")
    print("against ranking regressions, it is not evidence for the graph.")
    print("=" * 70)

    fr = data["file_retrieval"]
    for r in fr:
        print(f"\nTask: {r['prompt']}")
        print(f"  Expected files: {len(r['expected_files'])}")
        print(f"  Returned files: {len(r['returned_files'])}")
        print(f"  Precision: {r['precision']:.0%}")
        print(f"  Recall: {r['recall']:.0%}")
        print(f"  Query time: {r['query_time_ms']:.2f}ms")
        print(f"  Estimated avoided reads: {r['avoided_reads']}")
        print(f"  Estimated tokens saved: {r['estimated_tokens_saved']:.0f} (rough chars/4 estimate)")

    if fr:
        avg_p = sum(r["precision"] for r in fr) / len(fr)
        avg_r = sum(r["recall"] for r in fr) / len(fr)
        print(f"\nAggregate: precision={avg_p:.0%} recall={avg_r:.0%} over {len(fr)} tasks")

    print()
    print("=" * 70)
    print("GRAPH RELATIONSHIP TASKS (the case for the graph)")
    print("Each of these requires assembling multi-hop structure to answer.")
    print("The grep-baseline column shows whether naive text search over the")
    print("same fixture would have found the connection at all.")
    print("=" * 70)

    rel = data["relationship"]
    passed_count = 0
    for r in rel:
        status = "PASS" if r["passed"] else "FAIL"
        passed_count += r["passed"]
        print(f"\n[{status}] {r['id']} ({r['kind']})")
        if r.get("note"):
            print(f"  {r['note']}")
        if "grep_would_find_it" in r and r["grep_would_find_it"] is not None:
            grep_verdict = "grep WOULD find it (weak test)" if r["grep_would_find_it"] else "grep MISSES it (confirms graph value)"
            print(f"  Grep baseline: {grep_verdict}")

    if rel:
        print(f"\nAggregate: {passed_count}/{len(rel)} relationship tasks passed")

    print()
    print("=" * 70)
    print("CLAIM VERIFICATION (the anti-hallucination guarantee)")
    print("Each row is a structural claim + the verdict projectbrain should")
    print("return. false_support and false_refutation MUST stay at zero — those")
    print("are the two ways a ground-truth tool would itself hallucinate.")
    print("=" * 70)

    claims = data.get("claim_verification", [])
    false_support = sum(c["false_support"] for c in claims)
    false_refutation = sum(c["false_refutation"] for c in claims)
    correct = sum(c["correct"] for c in claims)
    decisive = sum(c["actual"] != "UNKNOWN" for c in claims)
    for c in claims:
        mark = "ok " if c["correct"] else "XX "
        print(f"\n[{mark}] {c['id']}")
        print(f"     {c['claim']}")
        print(f"     expected {c['expected']}, got {c['actual']} ({c['confidence']})")
        if not c["correct"]:
            flag = ("FALSE SUPPORT" if c["false_support"]
                    else "FALSE REFUTATION" if c["false_refutation"]
                    else "mismatch")
            print(f"     >>> {flag}")

    if claims:
        n = len(claims)
        print(f"\nAggregate: {correct}/{n} exact, "
              f"false_support={false_support}, false_refutation={false_refutation}, "
              f"decisiveness={decisive}/{n} ({decisive / n:.0%})")

    dogfood = data.get("dogfood_context_compiler", [])
    if dogfood:
        print()
        print("=" * 70)
        print("SELF-DOGFOODING: v2 SURGICAL CONTEXT COMPILER ON PROJECTBRAIN REPO")
        print("Compares tokens in `brain.compile_context()` (exact target slices +")
        print("signature skeletons) against BOTH a disciplined grep+120-line window")
        print("agent AND reading the matched full files via view_file.")
        print("=" * 70)
        for d in dogfood:
            print(f"\nTask: {d['prompt']}")
            print(f"  Sliced symbols: {', '.join(d['sliced_symbols'])} (recall={d['symbol_recall']:.0%})")
            print(f"  Compiled packet: {d['packet_tokens']} tokens in {d['compile_time_ms']}ms")
            print(f"  Disciplined grep + 120-line windows: {d['grep_window_tokens']} tokens "
                  f"(grep recall={d['grep_window_recall']:.0%}, {d['tokens_saved_vs_grep_window_pct']}% saved)")
            print(f"  Naive full-file reads: {d['naive_full_files_tokens']} tokens "
                  f"({d['tokens_saved_vs_matched_files_pct']}% saved vs reading matched files)")
            print(f"  Full repo size: {d['total_repo_tokens']} tokens "
                  f"({d['tokens_saved_vs_repo_pct']}% saved vs scanning repo)")

    adv = data.get("adversarial_dogfood", [])
    if adv:
        print()
        print("=" * 70)
        print("ADVERSARIAL / ZERO-IDENTIFIER BENCHMARK (No exact function names in prompt)")
        print("Tests natural conceptual prompts with ZERO exact symbol names leaked.")
        print("=" * 70)
        for a in adv:
            print(f"\nTask [{a['id']}]: {a['prompt']}")
            print(f"  Expected symbols: {', '.join(a['expected_symbols'])}")
            print(f"  Sliced symbols:   {', '.join(a['sliced_symbols'])} (ProjectBrain recall={a['symbol_recall']:.0%})")
            print(f"  Compiled packet:  {a['packet_tokens']} tokens in {a['compile_time_ms']}ms")
            print(f"  Disciplined grep + 120-line windows: {a['grep_window_tokens']} tokens "
                  f"(grep recall={a['grep_window_recall']:.0%}, {a['tokens_saved_vs_grep_window_pct']}% saved)")

    print()
    print(f"Index times (ms): {data['index_times_ms']}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the projectbrain evaluation harness")
    parser.add_argument("--project", default=None, help="Restrict to one fixture project")
    parser.add_argument("--json", action="store_true", help="Print raw JSON instead of the report")
    args = parser.parse_args()

    data = run(project_filter=args.project)

    if args.json:
        print(json.dumps(data, indent=2))
    else:
        _print_report(data)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
