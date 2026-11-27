"""Build igraph from extracted symbols, run Leiden, report coupling.

Two graph views:
  - file-level: nodes are files, edges weighted by cross-file reference count.
    This is what Leiden clusters. Answers "which files belong together?"
  - symbol-level: nodes are symbols, edges are references/calls.
    Useful for drilling into a cluster.

Symbol nodes are identified by ``<file>::<qualified_name>`` (file relative to
root when given), because qualified names alone repeat across files.
"""

from __future__ import annotations

import json
import logging
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

import igraph as ig
import leidenalg

from codegraph.extract import INTERESTING_KINDS, Symbol, SymbolGraph, resolve_edges, uri_to_path

log = logging.getLogger(__name__)

Resolved = list[tuple[Symbol, Symbol, str]]


@dataclass
class CouplingPair:
    source: str
    target: str
    weight: int


@dataclass
class Community:
    id: int
    files: list[str]
    internal_edges: int
    languages: list[str]


@dataclass
class AnalysisResult:
    communities: list[Community]
    modularity: float
    top_coupling: list[CouplingPair]
    node_count: int
    edge_count: int
    symbol_count: int
    symbol_graph: list[dict] = field(default_factory=list)
    symbol_communities: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "summary": {
                "files": self.node_count,
                "edges": self.edge_count,
                "symbols": self.symbol_count,
                "communities": len(self.communities),
                "modularity": round(self.modularity, 4),
            },
            "communities": [
                {
                    "id": c.id,
                    "size": len(c.files),
                    "internal_edges": c.internal_edges,
                    "languages": c.languages,
                    "files": c.files,
                }
                for c in self.communities
            ],
            "top_coupling": [
                {"source": c.source, "target": c.target, "weight": c.weight}
                for c in self.top_coupling
            ],
            "symbol_graph": self.symbol_graph,
            "symbol_communities": self.symbol_communities,
        }

    def print_summary(self, root: Path | None = None) -> None:
        print(f"\n{'='*60}")
        print(f"  Files: {self.node_count}  Edges: {self.edge_count}  "
              f"Symbols: {self.symbol_count}")
        print(f"  Communities: {len(self.communities)}  "
              f"Modularity: {self.modularity:.4f}")
        print(f"{'='*60}")

        for c in sorted(self.communities, key=lambda c: -len(c.files)):
            langs = ", ".join(c.languages)
            print(f"\n  Community {c.id} ({len(c.files)} files, "
                  f"{c.internal_edges} internal edges) [{langs}]")
            for f in sorted(c.files)[:15]:
                print(f"    {_relative(f, root)}")
            if len(c.files) > 15:
                print(f"    ... and {len(c.files) - 15} more")

        if self.top_coupling:
            print("\n  Top coupling pairs:")
            for cp in self.top_coupling[:10]:
                s = _relative(cp.source, root)
                t = _relative(cp.target, root)
                print(f"    {s}  <->  {t}  (weight={cp.weight})")

        if self.symbol_communities:
            print(f"\n  Symbol communities ({len(self.symbol_communities)}):")
            for sc in self.symbol_communities[:10]:
                print(f"\n    Community {sc['id']} "
                      f"({sc['size']} symbols, cohesion={sc['cohesion']})")
                for s in sc["symbols"][:8]:
                    print(f"      {s['kind']:12s} {s['name']:40s} {s['file']}")
                if sc["size"] > 8:
                    print(f"      ... and {sc['size'] - 8} more")

        print()


def _relative(uri_or_path: str, root: Path | None) -> str:
    """Decoded filesystem path, relative to root when it lies inside it."""
    path = uri_to_path(uri_or_path)
    if root:
        try:
            return str(Path(path).relative_to(root.resolve()))
        except ValueError:
            pass
    return path


def _symbol_id(sym: Symbol, root: Path | None) -> str:
    return f"{_relative(sym.uri, root)}::{sym.qualified_name}"


def _interesting_symbols(sg: SymbolGraph) -> list[Symbol]:
    return [s for s in sg.symbols if s.kind in INTERESTING_KINDS]


def _leiden(g: ig.Graph, resolution: float) -> leidenalg.VertexPartition.MutableVertexPartition:
    """Leiden on a weighted graph: modularity at resolution 1, RB-configuration otherwise."""
    partition_type = leidenalg.ModularityVertexPartition
    kwargs: dict = {
        "weights": "weight" if g.ecount() > 0 else None,
        "n_iterations": -1,
        "seed": 42,
    }
    if resolution != 1.0:
        partition_type = leidenalg.RBConfigurationVertexPartition
        kwargs["resolution_parameter"] = resolution
    return leidenalg.find_partition(g, partition_type, **kwargs)


def build_file_graph(sg: SymbolGraph) -> ig.Graph:
    """Collapse symbol graph to file-level coupling graph."""
    file_edges: Counter[tuple[str, str]] = Counter()
    for edge in sg.edges:
        src = edge.source.uri
        tgt = edge.target_uri
        if src != tgt and src and tgt:
            # normalize edge direction for undirected coupling
            file_edges[(min(src, tgt), max(src, tgt))] += 1

    all_files = sorted({s.uri for s in sg.symbols})
    if not all_files:
        return ig.Graph()

    g = ig.Graph(n=len(all_files), directed=False)
    g.vs["name"] = all_files
    idx = {f: i for i, f in enumerate(all_files)}

    edges = []
    weights = []
    for (s, t), w in file_edges.items():
        if s in idx and t in idx:
            edges.append((idx[s], idx[t]))
            weights.append(w)

    if edges:
        g.add_edges(edges)
        g.es["weight"] = weights

    return g


def build_symbol_graph(
    sg: SymbolGraph, root: Path | None = None, resolved: Resolved | None = None,
) -> ig.Graph:
    """Symbol-level graph — nodes are functions/classes, edges point dependent -> dependency."""
    if resolved is None:
        resolved = resolve_edges(sg)

    all_syms = _interesting_symbols(sg)
    if not all_syms:
        return ig.Graph()

    g = ig.Graph(directed=True)
    g.add_vertices(len(all_syms))
    g.vs["name"] = [s.qualified_name for s in all_syms]
    g.vs["id"] = [_symbol_id(s, root) for s in all_syms]
    g.vs["uri"] = [s.uri for s in all_syms]
    g.vs["kind"] = [s.kind_name for s in all_syms]
    g.vs["language"] = [s.language for s in all_syms]

    idx = {id(s): i for i, s in enumerate(all_syms)}
    edge_weights: Counter[tuple[int, int]] = Counter()
    edge_types: dict[tuple[int, int], str] = {}
    for src, tgt, etype in resolved:
        si, ti = idx.get(id(src)), idx.get(id(tgt))
        if si is not None and ti is not None and si != ti:
            key = (si, ti)
            edge_weights[key] += 1
            # a call is the stronger, more specific relation: keep it if seen
            if edge_types.get(key) != "call":
                edge_types[key] = etype

    if edge_weights:
        edges = list(edge_weights.keys())
        g.add_edges(edges)
        g.es["weight"] = [edge_weights[e] for e in edges]
        g.es["type"] = [edge_types[e] for e in edges]

    return g


def _symbol_edge_dicts(g: ig.Graph) -> list[dict]:
    out = []
    for e in g.es:
        src, tgt = g.vs[e.source], g.vs[e.target]
        out.append({
            "source": src["name"],
            "target": tgt["name"],
            "source_id": src["id"],
            "target_id": tgt["id"],
            "source_kind": src["kind"],
            "target_kind": tgt["kind"],
            "weight": e["weight"],
            "type": e["type"],
        })
    return out


def analyze(
    sg: SymbolGraph,
    root: Path | None = None,
    resolution: float = 1.0,
) -> AnalysisResult:
    """Run Leiden community detection on the file coupling graph."""
    g = build_file_graph(sg)

    if g.vcount() == 0:
        return AnalysisResult(
            communities=[], modularity=0.0, top_coupling=[],
            node_count=0, edge_count=0, symbol_count=len(sg.symbols),
        )

    resolved = resolve_edges(sg)
    symbol_edges = _symbol_edge_dicts(build_symbol_graph(sg, root=root, resolved=resolved))

    partition = _leiden(g, resolution)

    # build community objects
    file_languages: dict[str, set[str]] = {}
    for sym in sg.symbols:
        file_languages.setdefault(sym.uri, set()).add(sym.language)

    communities = []
    for cid, members in enumerate(partition):
        files = [g.vs[m]["name"] for m in members]
        langs = sorted({
            lang
            for f in files
            for lang in file_languages.get(f, set())
        })
        communities.append(Community(
            id=cid, files=files,
            internal_edges=g.subgraph(members).ecount(), languages=langs,
        ))

    # top coupling pairs
    coupling = [
        CouplingPair(
            source=g.vs[e.source]["name"],
            target=g.vs[e.target]["name"],
            weight=e["weight"],
        )
        for e in g.es
    ]
    coupling.sort(key=lambda c: -c.weight)

    symbol_communities = analyze_symbols(sg, root=root, resolution=resolution, resolved=resolved)

    return AnalysisResult(
        communities=communities,
        # leidenalg types this as Optional; it is always computed after find_partition
        modularity=partition.modularity or 0.0,
        top_coupling=coupling[:20],
        node_count=g.vcount(),
        edge_count=g.ecount(),
        symbol_count=len(sg.symbols),
        symbol_graph=symbol_edges,
        symbol_communities=symbol_communities,
    )


def save_report(result: AnalysisResult, path: Path) -> None:
    path.write_text(json.dumps(result.to_dict(), indent=2))
    log.info("Report written to %s", path)


def analyze_symbols(
    sg: SymbolGraph,
    root: Path | None = None,
    resolution: float = 1.0,
    call_weight: float = 3.0,
    ref_weight: float = 1.0,
    resolved: Resolved | None = None,
) -> list[dict]:
    """Leiden on the symbol-level graph. Returns communities of functions/classes."""
    if resolved is None:
        resolved = resolve_edges(sg)

    all_syms = _interesting_symbols(sg)
    if not all_syms:
        return []

    idx = {id(s): i for i, s in enumerate(all_syms)}
    edge_data: dict[tuple[int, int], float] = {}
    for src, tgt, etype in resolved:
        si, ti = idx.get(id(src)), idx.get(id(tgt))
        if si is None or ti is None or si == ti:
            continue
        key = (min(si, ti), max(si, ti))
        w = call_weight if etype == "call" else ref_weight
        edge_data[key] = edge_data.get(key, 0.0) + w

    if not edge_data:
        return []

    g = ig.Graph(n=len(all_syms), directed=False)
    edges = list(edge_data.keys())
    g.add_edges(edges)
    g.es["weight"] = [edge_data[e] for e in edges]

    partition = _leiden(g, resolution)

    communities = []
    for cid, members in enumerate(partition):
        syms = [
            {
                "id": _symbol_id(all_syms[m], root),
                "name": all_syms[m].qualified_name,
                "kind": all_syms[m].kind_name,
                "file": _relative(all_syms[m].uri, root),
            }
            for m in members
        ]
        # intra-community edge weight
        subg = g.subgraph(members)
        cohesion = sum(subg.es["weight"]) if subg.ecount() > 0 else 0
        communities.append({
            "id": cid,
            "size": len(members),
            "cohesion": round(cohesion, 1),
            "symbols": sorted(syms, key=lambda s: (s["file"], s["name"])),
        })

    communities.sort(key=lambda c: -c["cohesion"])
    return communities
