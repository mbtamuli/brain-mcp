"""
Brain CLI dispatcher -- routes subcommands to their implementations.
"""
import argparse
import json
import os
import sys
from pathlib import Path

# Ensure the package directory is on the path when invoked directly
_pkg_dir = Path(__file__).parent
if str(_pkg_dir) not in sys.path:
    sys.path.insert(0, str(_pkg_dir))


def cmd_build(args):
    from build import build_index
    result = build_index(full=args.full)
    for source, count in result["sources"].items():
        print(f"  {source}: {count} entries indexed")
    print(f"Build complete. {result['total']} entries in {result['db']}")


def cmd_search(args):
    from query import query_brain
    print(query_brain(args.query, limit=args.limit))


def cmd_write(args):
    from write import BrainStore
    store = BrainStore()
    if args.action == "add":
        if not args.content:
            print("Error: --content is required for add", file=sys.stderr)
            sys.exit(1)
        result = store.add(args.target, args.content, force=args.force)
    elif args.action == "replace":
        if not args.old_text or not args.content:
            print("Error: --old-text and --content are required for replace", file=sys.stderr)
            sys.exit(1)
        result = store.replace(args.target, args.old_text, args.content, force=args.force)
    elif args.action == "remove":
        if not args.old_text:
            print("Error: --old-text is required for remove", file=sys.stderr)
            sys.exit(1)
        result = store.remove(args.target, args.old_text)
    print(json.dumps(result, indent=2, ensure_ascii=False))


def cmd_serve(args):
    from serve import run_mcp_server
    run_mcp_server(verbose=args.verbose)


def cmd_status(args):
    brain_dir = Path(os.environ.get("BRAIN_DIR", str(Path.home() / "brain")))
    db_path = brain_dir / ".index" / "brain.db"
    delimiter = "\n\u00a7\n"

    def entry_count(path: Path) -> int:
        if not path.exists():
            return 0
        raw = path.read_text(encoding="utf-8").strip()
        if not raw:
            return 0
        return len([e for e in raw.split(delimiter) if e.strip()])

    memory_file = brain_dir / "MEMORY.md"
    user_file = brain_dir / "USER.md"

    print(f"Brain:     {brain_dir}")
    print(f"MEMORY.md: {entry_count(memory_file)} entries  ({memory_file.stat().st_size if memory_file.exists() else 0} bytes)")
    print(f"USER.md:   {entry_count(user_file)} entries  ({user_file.stat().st_size if user_file.exists() else 0} bytes)")
    print(f"Index:     {'exists' if db_path.exists() else 'not built -- run: brain build'}")


def main():
    parser = argparse.ArgumentParser(prog="brain", description="Brain access layer")
    sub = parser.add_subparsers(dest="command", required=True)

    # build
    p_build = sub.add_parser("build", help="Build FTS5 index from MEMORY.md and USER.md")
    p_build.add_argument("--full", action="store_true", help="Force full rebuild (drop + recreate)")

    # search
    p_search = sub.add_parser("search", help="Search brain entries via FTS5")
    p_search.add_argument("query", help="FTS5 query string")
    p_search.add_argument("--limit", type=int, default=10, help="Max results (default: 10)")

    # write
    p_write = sub.add_parser("write", help="Write to brain (add/replace/remove)")
    p_write.add_argument("action", choices=["add", "replace", "remove"])
    p_write.add_argument("--target", default="memory", choices=["memory", "user"])
    p_write.add_argument("--content", help="Entry content (required for add/replace)")
    p_write.add_argument("--old-text", dest="old_text", help="Substring to match (required for replace/remove)")
    p_write.add_argument("--force", action="store_true", help="Bypass size limit and security scan")

    # serve
    p_serve = sub.add_parser("serve", help="Start MCP server on stdio")
    p_serve.add_argument("--verbose", action="store_true")

    # status
    sub.add_parser("status", help="Show brain status")

    args = parser.parse_args()

    dispatch = {
        "build": cmd_build,
        "search": cmd_search,
        "write": cmd_write,
        "serve": cmd_serve,
        "status": cmd_status,
    }
    dispatch[args.command](args)


if __name__ == "__main__":
    main()
