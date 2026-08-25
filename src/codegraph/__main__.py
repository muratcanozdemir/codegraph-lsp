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
from pathlib import Path

from codegraph.extract import Extractor, SymbolGraph
from codegraph.graph import analyze, save_report
from codegraph.lsp import LSPClient

log = logging.getLogger(__name__)

# language_id -> (binary, args)
LSP_SERVERS: dict[str, tuple[str, list[str]]] = {
    "python": ("pyright-langserver", ["--stdio"]),
    "go": ("gopls", []),
}


def _find_servers() -> dict[str, tuple[str, list[str]]]:
    """Return only servers whose binary is on PATH."""
    available = {}
    for lang, (binary, args) in LSP_SERVERS.items():
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


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="codegraph",
        description="LSP-backed codebase coupling analysis with Leiden clustering",
    )
    parser.add_argument("root", type=Path, help="Repository root")
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
        help="Only re-extract these files (incremental mode)",
    )
    parser.add_argument(
        "--resolution", type=float, default=1.0,
        help="Leiden resolution parameter (higher = more communities)",
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

    servers = _find_servers()
    if not servers:
        print("Error: no LSP servers found (need pyright-langserver and/or gopls)", file=sys.stderr)
        sys.exit(1)

    clients = _start_clients(servers, root)
    try:
        extractor = Extractor(clients, root)

        if args.diff:
            # incremental mode
            if not args.load_graph or not args.load_graph.exists():
                print("Error: --diff requires --load-graph with existing graph", file=sys.stderr)
                sys.exit(1)
            base = _load_graph(args.load_graph)
            changed = [root / f for f in args.diff]
            delta = extractor.extract(files=changed)
            changed_uris = {f.resolve().as_uri() for f in changed}
            sg = _merge_incremental(base, delta, changed_uris)
            log.info("Incremental merge: %d changed files", len(changed))
        else:
            sg = extractor.extract()

        if args.save_graph:
            _save_graph(sg, args.save_graph)

    finally:
        _stop_clients(clients)

    result = analyze(sg, root=root, resolution=args.resolution)
    result.print_summary(root=root)

    if args.output:
        save_report(result, args.output)


if __name__ == "__main__":
    main()
