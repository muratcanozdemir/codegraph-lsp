"""Extract symbols and references from LSP servers into a uniform graph model.

Handles both hierarchical (DocumentSymbol) and flat (SymbolInformation)
responses. Flattens nested symbols (e.g. methods inside classes) into
a single list with qualified names.
"""

from __future__ import annotations

import bisect
import fnmatch
import logging
import os
import sys
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import unquote, urlparse

from codegraph.lsp import LSPClient

log = logging.getLogger(__name__)

# LSP SymbolKind enum (subset we care about)
SYMBOL_KIND_NAMES = {
    1: "file", 2: "module", 3: "namespace", 4: "package",
    5: "class", 6: "method", 7: "property", 8: "field",
    9: "constructor", 10: "enum", 11: "interface", 12: "function",
    13: "variable", 14: "constant", 15: "string", 16: "number",
    23: "struct", 24: "event", 25: "operator", 26: "type_parameter",
}

# Only extract symbols likely to produce meaningful coupling edges
INTERESTING_KINDS = {5, 6, 9, 11, 12, 23}  # class, method, constructor, interface, function, struct

# Directories never worth indexing (vendored, generated, environments).
SKIP_DIRS = {"vendor", "node_modules", ".git", "__pycache__", ".venv", "venv"}

# Test code is excluded by default: tests, fixtures and fake servers couple
# to everything they exercise and drown out the source's own structure.
TEST_DIRS = {"tests", "test", "testdata"}
TEST_FILE_PATTERNS = ("test_*.py", "*_test.py", "conftest.py", "*_test.go")


def is_test_path(rel: Path) -> bool:
    """True for files under a test directory or named like a test module."""
    if any(part in TEST_DIRS for part in rel.parts[:-1]):
        return True
    return any(fnmatch.fnmatchcase(rel.name, pat) for pat in TEST_FILE_PATTERNS)


# end_line value for symbols whose extent is unknown (graphs saved before
# extents were recorded).
UNKNOWN_END = -1


def uri_to_path(uri: str) -> str:
    """file:// URI -> filesystem path, percent-decoded. Non-URIs pass through."""
    if not uri.startswith("file://"):
        return uri
    return unquote(urlparse(uri).path)


@dataclass(frozen=True)
class Symbol:
    uri: str
    name: str
    qualified_name: str
    kind: int
    line: int
    character: int
    language: str
    end_line: int = UNKNOWN_END  # last line of the symbol's full range

    @property
    def kind_name(self) -> str:
        return SYMBOL_KIND_NAMES.get(self.kind, "unknown")

    @property
    def file_path(self) -> str:
        return uri_to_path(self.uri)

    def contains_line(self, line: int) -> bool:
        if self.end_line == UNKNOWN_END:
            return line >= self.line
        return self.line <= line <= self.end_line


@dataclass
class Edge:
    """A raw LSP observation, before resolution to symbols.

    "call": ``source`` calls whatever lives at the target location.
    "reference": ``source`` is *used* at the target location (the target
    depends on ``source``). resolve_edges() flips these so resolved edges
    always point from dependent to dependency.
    """

    source: Symbol
    target_uri: str
    target_line: int
    edge_type: str  # "reference" or "call"


@dataclass
class SymbolGraph:
    symbols: list[Symbol] = field(default_factory=list)
    edges: list[Edge] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "symbols": [
                {
                    "uri": s.uri, "name": s.name,
                    "qualified_name": s.qualified_name,
                    "kind": s.kind, "kind_name": s.kind_name,
                    "language": s.language,
                    "line": s.line, "character": s.character,
                    "end_line": s.end_line,
                }
                for s in self.symbols
            ],
            "edges": [
                {
                    "source_qualified": e.source.qualified_name,
                    "source_uri": e.source.uri,
                    "target_uri": e.target_uri,
                    "target_line": e.target_line,
                    "type": e.edge_type,
                }
                for e in self.edges
            ],
        }

    @classmethod
    def from_dict(cls, data: dict, language: str = "") -> SymbolGraph:
        """Rebuild a graph from to_dict() output.

        ``language`` is only a fallback for graphs saved without a per-symbol
        language field.
        """
        symbols = [
            Symbol(
                uri=s["uri"], name=s["name"],
                qualified_name=s["qualified_name"],
                kind=s.get("kind", 0), line=s["line"],
                character=s.get("character", 0),
                language=s.get("language") or language,
                end_line=s.get("end_line", UNKNOWN_END),
            )
            for s in data.get("symbols", [])
        ]
        # qualified names are only unique within a file (e.g. `main` in many
        # Go packages), so edges are matched on (uri, qualified_name).
        sym_by_key = {(s.uri, s.qualified_name): s for s in symbols}
        edges = []
        for e in data.get("edges", []):
            src = sym_by_key.get((e.get("source_uri", ""), e["source_qualified"]))
            if src:
                edges.append(Edge(
                    source=src,
                    target_uri=e["target_uri"],
                    target_line=e["target_line"],
                    edge_type=e["type"],
                ))
        return cls(symbols=symbols, edges=edges)


class Extractor:
    """Drives LSP servers to extract a full symbol graph."""

    EXTENSIONS: dict[str, list[str]] = {
        "python": [".py"],
        "go": [".go"],
    }

    def __init__(
        self,
        clients: dict[str, LSPClient],
        root: Path,
        jobs: int = 8,
        include_tests: bool = False,
        exclude: list[str] | None = None,
    ) -> None:
        self._clients = clients  # language_id -> client
        self._root = root.resolve()
        self._jobs = max(1, jobs)
        self._include_tests = include_tests
        self._exclude = list(exclude or [])

    def _excluded(self, path: Path) -> bool:
        """Whether a file is filtered out (tests, or an --exclude glob).

        Globs match the root-relative path (``*`` crosses ``/``) or any single
        path component, so ``examples/*`` and ``fakes`` both work.
        """
        try:
            rel = path.resolve().relative_to(self._root)
        except ValueError:
            rel = Path(path.name)
        if not self._include_tests and is_test_path(rel):
            return True
        posix = rel.as_posix()
        return any(
            fnmatch.fnmatchcase(posix, pat)
            or any(fnmatch.fnmatchcase(part, pat) for part in rel.parts)
            for pat in self._exclude
        )

    def extract(self, files: list[Path] | None = None) -> SymbolGraph:
        graph = SymbolGraph()
        with ThreadPoolExecutor(max_workers=self._jobs) as pool:
            for lang, client in self._clients.items():
                if files is not None:
                    lang_files = [
                        f for f in files
                        if f.suffix in self.EXTENSIONS.get(lang, [])
                        and not self._excluded(f)
                    ]
                else:
                    lang_files = list(self._discover(lang))

                total = len(lang_files)
                log.info("Extracting %d %s files", total, lang)

                for i, fpath in enumerate(lang_files, 1):
                    self._extract_file(client, fpath, lang, graph, pool)
                    if i % 50 == 0 or i == total:
                        print(f"  [{lang}] {i}/{total}", file=sys.stderr)

        return graph

    def _discover(self, language: str) -> Iterator[Path]:
        exts = tuple(self.EXTENSIONS.get(language, []))
        if not exts:
            return
        for dirpath, dirnames, filenames in os.walk(self._root):
            # prune in place so os.walk never descends into skipped trees
            dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS)
            for name in sorted(filenames):
                # skip generated protobuf
                if not name.endswith(exts) or name.endswith(".pb.go"):
                    continue
                path = Path(dirpath) / name
                if not self._excluded(path):
                    yield path

    def _extract_file(
        self,
        client: LSPClient,
        fpath: Path,
        language: str,
        graph: SymbolGraph,
        pool: ThreadPoolExecutor | None = None,
    ) -> None:
        uri = fpath.resolve().as_uri()
        try:
            text = fpath.read_text(errors="replace")
        except OSError as exc:
            log.warning("Cannot read %s: %s", fpath, exc)
            return

        client.open_document(uri, text)
        try:
            raw_symbols = client.document_symbols(uri)
            symbols = list(self._flatten_symbols(raw_symbols, uri, language))
            graph.symbols.extend(symbols)

            interesting = [s for s in symbols if s.kind in INTERESTING_KINDS]

            def edges_for(sym: Symbol) -> list[Edge]:
                return self._extract_references(client, sym) + self._extract_calls(client, sym)

            # LSPClient multiplexes concurrent requests, so per-symbol lookups
            # run in parallel; map() keeps the edge order deterministic.
            results = pool.map(edges_for, interesting) if pool else map(edges_for, interesting)
            for edges in results:
                graph.edges.extend(edges)
        except Exception as exc:
            log.warning("Error extracting %s: %s", fpath, exc)
        finally:
            client.close_document(uri)

    def _flatten_symbols(
        self,
        items: list[dict],
        uri: str,
        language: str,
        prefix: str = "",
    ) -> Iterator[Symbol]:
        for item in items:
            name = item.get("name", "")
            qn = f"{prefix}.{name}" if prefix else name

            # DocumentSymbol has selectionRange; SymbolInformation has location
            if "selectionRange" in item:
                pos = item["selectionRange"]["start"]
                full = item.get("range", item["selectionRange"])
            elif "location" in item:
                full = item["location"]["range"]
                pos = full["start"]
                uri = item["location"].get("uri", uri)
            else:
                continue

            sym = Symbol(
                uri=uri, name=name, qualified_name=qn,
                kind=item.get("kind", 0),
                line=pos["line"], character=pos["character"],
                language=language,
                end_line=full.get("end", {}).get("line", UNKNOWN_END),
            )
            yield sym

            # recurse into children (DocumentSymbol)
            for child in item.get("children", []):
                yield from self._flatten_symbols([child], uri, language, prefix=qn)

    def _extract_references(self, client: LSPClient, sym: Symbol) -> list[Edge]:
        if not client.capabilities.references:
            return []
        try:
            refs = client.references(sym.uri, sym.line, sym.character)
        except Exception:
            return []
        return [
            Edge(
                source=sym,
                target_uri=ref["uri"],
                target_line=ref["range"]["start"]["line"],
                edge_type="reference",
            )
            for ref in refs
        ]

    def _extract_calls(self, client: LSPClient, sym: Symbol) -> list[Edge]:
        if not client.capabilities.call_hierarchy:
            return []
        try:
            items = client.prepare_call_hierarchy(sym.uri, sym.line, sym.character)
        except Exception:
            return []
        edges = []
        for item in items:
            try:
                outgoing = client.outgoing_calls(item)
            except Exception:
                continue
            for call in outgoing:
                to = call.get("to", {})
                edges.append(Edge(
                    source=sym,
                    target_uri=to.get("uri", ""),
                    target_line=to.get("range", {}).get("start", {}).get("line", 0),
                    edge_type="call",
                ))
        return edges


def resolve_edges(sg: SymbolGraph) -> list[tuple[Symbol, Symbol, str]]:
    """Map (uri, line) edge targets to the innermost symbol containing that line.

    Returned tuples are (dependent, dependency, type): call edges keep their
    caller -> callee direction, reference edges are flipped from
    definition -> use site to use site -> definition. Locations outside every
    interesting symbol (e.g. module-level code) are dropped.
    """
    by_file: dict[str, list[Symbol]] = {}
    for s in sg.symbols:
        if s.kind in INTERESTING_KINDS:
            by_file.setdefault(s.uri, []).append(s)
    starts: dict[str, list[int]] = {}
    for uri, syms in by_file.items():
        syms.sort(key=lambda s: s.line)
        starts[uri] = [s.line for s in syms]

    resolved = []
    for edge in sg.edges:
        candidates = by_file.get(edge.target_uri)
        if not candidates:
            continue
        # walk back from the last symbol starting at/before the line; the
        # first one whose range contains it is the innermost (nested symbols
        # start later than their parents).
        best = None
        for i in range(bisect.bisect_right(starts[edge.target_uri], edge.target_line) - 1, -1, -1):
            if candidates[i].contains_line(edge.target_line):
                best = candidates[i]
                break
        if best is None or best == edge.source:
            continue
        if edge.edge_type == "reference":
            resolved.append((best, edge.source, edge.edge_type))
        else:
            resolved.append((edge.source, best, edge.edge_type))
    return resolved
