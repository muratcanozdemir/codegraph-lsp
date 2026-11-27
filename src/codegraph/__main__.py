"""CLI for codegraph — LSP-backed codebase coupling analysis.

Usage:
    # Full extraction
    codegraph /path/to/repo

    # Full extraction, save graph + report
    codegraph /path/to/repo -o report.json --save-graph graph.json

    # Incremental: re-extract only changed files, merge with saved graph
    codegraph /path/to/repo --diff src/api.py src/handler.go --load-graph graph.json

    # Adjust Leiden resolution (higher = more communities)
    codegraph /path/to/repo --resolution 1.5
"""

from __future__ import annotations

import argparse
import json
import logging
import shutil
import sys
from collections.abc import Iterable
from pathlib import Path

from codegraph import __version__
from codegraph.extract import Extractor, SymbolGraph
from codegraph.graph import analyze, save_report
from codegraph.lsp import LSPClient

log = logging.getLogger(__name__)

# language_id -> (binary, args)
LSP_SERVERS: dict[str, tuple[str, list[str]]] = {
    "python": ("pyright-langserver", ["--stdio"]),
    "go": ("gopls", []),
}


def _find_servers(languages: Iterable[str] | None = None) -> dict[str, tuple[str, list[str]]]:
    """Servers whose binary is on PATH, for these languages (default: all known)."""
    wanted = set(LSP_SERVERS if languages is None else languages)
    available = {}
    for lang, (binary, args) in LSP_SERVERS.items():
        if lang not in wanted:
            continue
        if shutil.which(binary):
            available[lang] = (binary, args)
        else:
            log.warning("%s not found on PATH — skipping %s", binary, lang)
    return available


def _start_clients(
    servers: dict[str, tuple[str, list[str]]], root: Path,
) -> dict[str, LSPClient]:
    clients = {}
    for lang, (binary, args) in servers.items():
        log.info("Starting %s (%s)", binary, lang)
        clients[lang] = LSPClient(
            cmd=[binary, *args], root=root, language_id=lang,
        )
    return clients


def _stop_clients(clients: dict[str, LSPClient]) -> None:
    for lang, client in clients.items():
        log.info("Shutting down %s", lang)
        client.shutdown()


def _load_graph(path: Path) -> SymbolGraph:
    data = json.loads(path.read_text())
    return SymbolGraph.from_dict(data)


def _save_graph(sg: SymbolGraph, path: Path) -> None:
    path.write_text(json.dumps(sg.to_dict(), indent=2))
    log.info("Graph saved to %s", path)


def _merge_incremental(
    base: SymbolGraph, delta: SymbolGraph, changed_uris: set[str],
) -> SymbolGraph:
    """Replace symbols/edges for changed files, keep the rest.

    Only edges *sourced* from a changed file are stale (delta re-extracts
    those). Edges from an unchanged file that reference a changed file are
    kept — delta extraction never re-visits unchanged files, so dropping
    them would silently lose real incoming coupling instead of just
    tolerating possibly-shifted target line numbers.
    """
    # keep symbols from files NOT in the changed set
    kept_symbols = [s for s in base.symbols if s.uri not in changed_uris]
    kept_edges = [e for e in base.edges if e.source.uri not in changed_uris]
    return SymbolGraph(
        symbols=kept_symbols + delta.symbols,
        edges=kept_edges + delta.edges,
    )


def _positive_int(value: str) -> int:
    n = int(value)
    if n < 1:
        raise argparse.ArgumentTypeError("must be >= 1")
    return n


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="codegraph",
        description="LSP-backed codebase coupling analysis with Leiden clustering",
    )
    parser.add_argument("root", type=Path, help="Repository root")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument(
        "-o", "--output", type=Path, default=None,
        help="Write JSON report to this path",
    )
    parser.add_argument(
        "--save-graph", type=Path, default=None,
        help="Persist raw symbol graph for incremental runs",
    )
    parser.add_argument(
        "--load-graph", type=Path, default=None,
        help="Load previous symbol graph (for --diff mode)",
    )
    parser.add_argument(
        "--diff", nargs="+", type=Path, default=None,
        help="Only re-extract these files (incremental mode); "
             "relative paths are resolved against ROOT, not the current directory",
    )
    parser.add_argument(
        "--resolution", type=float, default=1.0,
        help="Leiden resolution parameter (higher = more communities)",
    )
    parser.add_argument(
        "--min-community-size", type=_positive_int, default=3, metavar="N",
        help="Fold symbol communities smaller than N into the community they are most "
             "linked to, or that shares their files (default: 3; 1 = keep all)",
    )
    parser.add_argument(
        "-j", "--jobs", type=_positive_int, default=8,
        help="Concurrent LSP lookups per file (default: 8; 1 = sequential)",
    )
    parser.add_argument(
        "--include-tests", action="store_true",
        help="Also index test code (tests/, test_*.py, conftest.py, *_test.go); "
             "excluded by default",
    )
    parser.add_argument(
        "--exclude", action="append", default=[], metavar="GLOB",
        help="Skip files whose ROOT-relative path, or any path component, "
             "matches GLOB (repeatable), e.g. --exclude 'examples/*' --exclude fakes",
    )
    parser.add_argument(
        "-v", "--verbose", action="store_true",
        help="Debug logging",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )

    root = args.root.resolve()
    if not root.is_dir():
        print(f"Error: {root} is not a directory", file=sys.stderr)
        sys.exit(1)

    if args.diff and (not args.load_graph or not args.load_graph.exists()):
        print("Error: --diff requires --load-graph with existing graph", file=sys.stderr)
        sys.exit(1)
    changed = [root / f for f in args.diff] if args.diff else None

    def extractor(clients: dict[str, LSPClient]) -> Extractor:
        return Extractor(
            clients, root, jobs=args.jobs,
            include_tests=args.include_tests, exclude=args.exclude,
        )

    # find the files first, so only the servers for languages present get started
    plan = extractor({}).plan(changed)
    if not plan and changed is None:
        exts = ", ".join(e for es in Extractor.EXTENSIONS.values() for e in es)
        print(f"Error: no source files ({exts}) under {root}", file=sys.stderr)
        sys.exit(1)
    servers = _find_servers(plan)
    if plan and not servers:
        need = ", ".join(LSP_SERVERS[lang][0] for lang in plan if lang in LSP_SERVERS)
        print(f"Error: no LSP servers found (need {need})", file=sys.stderr)
        sys.exit(1)

    clients = _start_clients(servers, root)
    try:
        if changed is not None:
            # incremental mode
            base = _load_graph(args.load_graph)
            delta = extractor(clients).extract(plan=plan)
            changed_uris = {f.resolve().as_uri() for f in changed}
            sg = _merge_incremental(base, delta, changed_uris)
            log.info("Incremental merge: %d changed files", len(changed))
        else:
            sg = extractor(clients).extract(plan=plan)

        if args.save_graph:
            _save_graph(sg, args.save_graph)

    finally:
        _stop_clients(clients)

    result = analyze(
        sg, root=root, resolution=args.resolution, min_community_size=args.min_community_size,
    )
    result.print_summary(root=root)

    if args.output:
        save_report(result, args.output)


if __name__ == "__main__":
    main()
