"""Extract symbols and references from LSP servers into a uniform graph model.

Handles both hierarchical (DocumentSymbol) and flat (SymbolInformation)
responses. Flattens nested symbols (e.g. methods inside classes) into
a single list with qualified names.
"""

from __future__ import annotations

import logging
import sys
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path

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


@dataclass(frozen=True)
class Symbol:
    uri: str
    name: str
    qualified_name: str
    kind: int
    line: int
    character: int
    language: str

    @property
    def kind_name(self) -> str:
        return SYMBOL_KIND_NAMES.get(self.kind, "unknown")

    @property
    def file_path(self) -> str:
        """Strip file:// prefix."""
        uri = self.uri
        if uri.startswith("file://"):
            uri = uri[7:]
        return uri


@dataclass
class Edge:
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
        symbols = [
            Symbol(
                uri=s["uri"], name=s["name"],
                qualified_name=s["qualified_name"],
                kind=s.get("kind", 0), line=s["line"],
                character=s.get("character", 0),
                language=language,
            )
            for s in data.get("symbols", [])
        ]
        sym_by_qn = {s.qualified_name: s for s in symbols}
        edges = []
        for e in data.get("edges", []):
            src = sym_by_qn.get(e["source_qualified"])
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

    def __init__(self, clients: dict[str, LSPClient], root: Path) -> None:
        self._clients = clients  # language_id -> client
        self._root = root.resolve()

    def extract(self, files: list[Path] | None = None) -> SymbolGraph:
        graph = SymbolGraph()
        for lang, client in self._clients.items():
            if files is not None:
                lang_files = [
                    f for f in files
                    if f.suffix in self.EXTENSIONS.get(lang, [])
                ]
            else:
                lang_files = list(self._discover(lang))

            total = len(lang_files)
            log.info("Extracting %d %s files", total, lang)

            for i, fpath in enumerate(lang_files, 1):
                self._extract_file(client, fpath, lang, graph)
                if i % 50 == 0 or i == total:
                    print(f"  [{lang}] {i}/{total}", file=sys.stderr)

        return graph

    def _discover(self, language: str) -> Iterator[Path]:
        exts = self.EXTENSIONS.get(language, [])
        for ext in exts:
            for p in self._root.rglob(f"*{ext}"):
                # skip vendored / generated dirs
                parts = p.relative_to(self._root).parts
                if any(d in parts for d in ("vendor", "node_modules", ".git", "__pycache__", ".venv", "venv")):
                    continue
                # skip generated protobuf (Go test files are kept — they reveal coupling)
                if p.name.endswith(".pb.go"):
                    continue
                yield p

    def _extract_file(
        self,
        client: LSPClient,
        fpath: Path,
        language: str,
        graph: SymbolGraph,
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

            for sym in symbols:
                if sym.kind not in INTERESTING_KINDS:
                    continue
                self._extract_references(client, sym, graph)
                self._extract_calls(client, sym, graph)
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
            elif "location" in item:
                pos = item["location"]["range"]["start"]
                uri = item["location"].get("uri", uri)
            else:
                continue

            sym = Symbol(
                uri=uri, name=name, qualified_name=qn,
                kind=item.get("kind", 0),
                line=pos["line"], character=pos["character"],
                language=language,
            )
            yield sym

            # recurse into children (DocumentSymbol)
            for child in item.get("children", []):
                yield from self._flatten_symbols([child], uri, language, prefix=qn)

    def _extract_references(
        self, client: LSPClient, sym: Symbol, graph: SymbolGraph,
    ) -> None:
        if not client.capabilities.references:
            return
        try:
            refs = client.references(sym.uri, sym.line, sym.character)
        except Exception:
            return
        for ref in refs:
            graph.edges.append(Edge(
                source=sym,
                target_uri=ref["uri"],
                target_line=ref["range"]["start"]["line"],
                edge_type="reference",
            ))

    def _extract_calls(
        self, client: LSPClient, sym: Symbol, graph: SymbolGraph,
    ) -> None:
        if not client.capabilities.call_hierarchy:
            return
        try:
            items = client.prepare_call_hierarchy(sym.uri, sym.line, sym.character)
        except Exception:
            return
        for item in items:
            try:
                outgoing = client.outgoing_calls(item)
            except Exception:
                continue
            for call in outgoing:
                to = call.get("to", {})
                graph.edges.append(Edge(
                    source=sym,
                    target_uri=to.get("uri", ""),
                    target_line=to.get("range", {}).get("start", {}).get("line", 0),
                    edge_type="call",
                ))


def resolve_edges(sg: SymbolGraph) -> list[tuple[Symbol, Symbol, str]]:
    """Map (uri, line) edge targets back to the nearest enclosing symbol."""
    by_file: dict[str, list[tuple[int, Symbol]]] = {}
    for s in sg.symbols:
        if s.kind not in INTERESTING_KINDS:
            continue
        by_file.setdefault(s.uri, []).append((s.line, s))
    for v in by_file.values():
        v.sort(key=lambda x: x[0])

    resolved = []
    for edge in sg.edges:
        candidates = by_file.get(edge.target_uri)
        if not candidates:
            continue
        best = None
        for line, sym in candidates:
            if line <= edge.target_line:
                best = sym
            else:
                break
        if best and best != edge.source:
            resolved.append((edge.source, best, edge.edge_type))
    return resolved