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

    return {
        "projects": projects,
        "index_times_ms": index_times,
        "file_retrieval": file_retrieval_results,
        "relationship": relationship_results,
        "claim_verification": claim_results,
    }


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
