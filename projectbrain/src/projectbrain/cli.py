import argparse
import json
import sys

from .brain import ProjectBrain


def _print_query_summary(result: dict) -> None:
    results = result.get("results", [])
    if not results:
        print("No matching files.")
        return

    print("Top files:")
    for i, r in enumerate(results, start=1):
        print(f"{i}. {r['rel_path']}")
        print(f"   Reason: {r['reason']}")

        conn_parts = []
        for c in r.get("connections", []):
            verb = "imports" if c["relation"] == "imports" else "used by"
            conn_parts.append(f"{verb} {c['rel_path']}")
        if conn_parts:
            print(f"   Connections: {', '.join(conn_parts)}")
        print()


def _print_context_summary(ctx: dict) -> None:
    if not ctx:
        print("No indexed file matched that path.")
        return

    print(ctx.get("rel_path", ctx.get("file_path", "?")))

    fns = [f["name"] for f in ctx.get("functions", [])]
    if fns:
        print(f"  Functions: {', '.join(fns)}")

    clss = [c["name"] for c in ctx.get("classes", [])]
    if clss:
        print(f"  Classes: {', '.join(clss)}")

    for r in ctx.get("routes", []):
        print(f"  Route: {r['method']} {r['path']}")

    if ctx.get("imports_resolved"):
        print(f"  Imports: {', '.join(ctx['imports_resolved'])}")

    if ctx.get("imported_by"):
        print(f"  Imported by: {', '.join(ctx['imported_by'])}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="projectbrain")
    parser.add_argument("--path", default=".", help="Project root (default: current directory)")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("index", help="Build and cache the index for the project")

    query_parser = sub.add_parser("query", help="Find files relevant to a task description")
    query_parser.add_argument("prompt", help="Natural language bug report or feature task")
    query_parser.add_argument("--top-n", type=int, default=10)
    query_group = query_parser.add_mutually_exclusive_group()
    query_group.add_argument("--json", action="store_true", help="Raw JSON output (default)")
    query_group.add_argument("--summary", action="store_true", help="Human-readable summary")

    context_parser = sub.add_parser("context", help="Get a file plus its 1-hop import chain")
    context_parser.add_argument("file", help="File path, relative to --path or absolute")
    context_group = context_parser.add_mutually_exclusive_group()
    context_group.add_argument("--json", action="store_true", help="Raw JSON output (default)")
    context_group.add_argument("--summary", action="store_true", help="Human-readable summary")

    verify_parser = sub.add_parser(
        "verify", help="Fact-check a claim: <subject> <relation> <object>")
    verify_parser.add_argument("subject", help="Function, class, file, or route")
    verify_parser.add_argument(
        "relation",
        help="calls | reaches | inherits | has_method | defines | imports | handles")
    verify_parser.add_argument("object", help="The name the relation targets")

    sub.add_parser("health", help="Report index coverage and blind spots")

    args = parser.parse_args(argv)
    brain = ProjectBrain(args.path)

    if args.command == "index":
        print(json.dumps(brain.index(), indent=2))
        return 0

    if args.command == "query":
        result = brain.query_with_graph(args.prompt, top_n=args.top_n)
        if args.summary:
            _print_query_summary(result)
        else:
            print(json.dumps(result, indent=2))
        return 0

    if args.command == "context":
        ctx = brain.get_context_for_file(args.file)
        if args.summary:
            _print_context_summary(ctx)
        else:
            print(json.dumps(ctx, indent=2))
        return 0

    if args.command == "verify":
        print(json.dumps(
            brain.verify_claim(args.subject, args.relation, args.object), indent=2))
        return 0

    if args.command == "health":
        print(json.dumps(brain.health(), indent=2))
        return 0

    return 1


if __name__ == "__main__":
    sys.exit(main())
