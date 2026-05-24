"""Build igraph from extracted symbols, run Leiden, report coupling.

Two graph views:
  - file-level: nodes are files, edges weighted by cross-file reference count.
    This is what Leiden clusters. Answers "which files belong together?"
  - symbol-level: nodes are symbols, edges are references/calls.
    Useful for drilling into a cluster.
"""

from __future__ import annotations

import json
import logging
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

import igraph as ig
import leidenalg

from codegraph.extract import SymbolGraph, resolve_edges, INTERESTING_KINDS


log = logging.getLogger(__name__)


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
                display = _relative(f, root) if root else f
                print(f"    {display}")
            if len(c.files) > 15:
                print(f"    ... and {len(c.files) - 15} more")

        if self.top_coupling:
            print(f"\n  Top coupling pairs:")
            for cp in self.top_coupling[:10]:
                s = _relative(cp.source, root) if root else cp.source
                t = _relative(cp.target, root) if root else cp.target
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
    path = uri_or_path
    if path.startswith("file://"):
        path = path[7:]
    if root:
        try:
            return str(Path(path).relative_to(root.resolve()))
        except ValueError:
            pass
    return path


def build_file_graph(sg: SymbolGraph) -> ig.Graph:
    """Collapse symbol graph to file-level coupling graph."""
    file_edges: Counter[tuple[str, str]] = Counter()
    for edge in sg.edges:
        src = edge.source.uri
        tgt = edge.target_uri
        if src != tgt and src and tgt:
            # normalize edge direction for undirected coupling
            pair = tuple(sorted([src, tgt]))
            file_edges[pair] += 1

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


def build_symbol_graph(sg: SymbolGraph) -> ig.Graph:
    """Symbol-level graph — nodes are functions/classes, edges are refs/calls."""
    resolved = resolve_edges(sg)

    all_syms = [s for s in sg.symbols if s.kind in {5, 6, 9, 11, 12, 23}]
    if not all_syms:
        return ig.Graph()

    g = ig.Graph(directed=True)
    g.add_vertices(len(all_syms))
    g.vs["name"] = [s.qualified_name for s in all_syms]
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
            edge_types[key] = etype  # last wins, fine

    if edge_weights:
        edges = list(edge_weights.keys())
        g.add_edges(edges)
        g.es["weight"] = [edge_weights[e] for e in edges]
        g.es["type"] = [edge_types[e] for e in edges]

    return g

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
            node_count=0, edge_count=0, symbol_count=len(sg.symbols), symbol_graph = [],
        )

    # Leiden with modularity optimization
    partition_type = leidenalg.ModularityVertexPartition
    kwargs = {
        "weights": "weight" if g.ecount() > 0 else None,
        "n_iterations": -1,
        "seed": 42,
    }
    if resolution != 1.0:
        partition_type = leidenalg.RBConfigurationVertexPartition
        kwargs["resolution_parameter"] = resolution

    sg_graph = build_symbol_graph(sg)
    symbol_edges = []
    if sg_graph.ecount() > 0:
        for e in sg_graph.es:
            symbol_edges.append({
                "source": sg_graph.vs[e.source]["name"],
                "target": sg_graph.vs[e.target]["name"],
                "source_kind": sg_graph.vs[e.source]["kind"],
                "target_kind": sg_graph.vs[e.target]["kind"],
                "weight": e["weight"],
                "type": e["type"],
            })

    partition = leidenalg.find_partition(g, partition_type, **kwargs)

    # build community objects
    file_languages: dict[str, set[str]] = {}
    for sym in sg.symbols:
        file_languages.setdefault(sym.uri, set()).add(sym.language)

    communities = []
    for cid, members in enumerate(partition):
        files = [g.vs[m]["name"] for m in members]
        # count internal edges
        subg = g.subgraph(members)
        internal = subg.ecount()
        langs = sorted({
            lang
            for f in files
            for lang in file_languages.get(f, set())
        })
        communities.append(Community(
            id=cid, files=files,
            internal_edges=internal, languages=langs,
        ))

    # top coupling pairs
    coupling = []
    if g.ecount() > 0:
        for e in g.es:
            coupling.append(CouplingPair(
                source=g.vs[e.source]["name"],
                target=g.vs[e.target]["name"],
                weight=e["weight"],
            ))
        coupling.sort(key=lambda c: -c.weight)

    symbol_communities = analyze_symbols(sg, root=root, resolution=resolution)

    return AnalysisResult(
        communities=communities,
        modularity=partition.modularity,
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
) -> list[dict]:
    """Leiden on the symbol-level graph. Returns communities of functions/classes."""
    resolved = resolve_edges(sg)

    all_syms = [s for s in sg.symbols if s.kind in INTERESTING_KINDS]
    if not all_syms:
        return []

    g = ig.Graph(directed=False)
    g.add_vertices(len(all_syms))
    g.vs["name"] = [s.qualified_name for s in all_syms]
    g.vs["uri"] = [s.uri for s in all_syms]
    g.vs["kind"] = [s.kind_name for s in all_syms]

    idx = {id(s): i for i, s in enumerate(all_syms)}
    edge_data: dict[tuple[int, int], float] = {}
    for src, tgt, etype in resolved:
        si, ti = idx.get(id(src)), idx.get(id(tgt))
        if si is None or ti is None or si == ti:
            continue
        key = tuple(sorted([si, ti]))
        w = call_weight if etype == "call" else ref_weight
        edge_data[key] = edge_data.get(key, 0.0) + w

    if not edge_data:
        return []

    edges = list(edge_data.keys())
    g.add_edges(edges)
    g.es["weight"] = [edge_data[e] for e in edges]

    partition_type = leidenalg.ModularityVertexPartition
    kwargs = {"weights": "weight", "n_iterations": -1, "seed": 42}
    if resolution != 1.0:
        partition_type = leidenalg.RBConfigurationVertexPartition
        kwargs["resolution_parameter"] = resolution

    partition = leidenalg.find_partition(g, partition_type, **kwargs)

    communities = []
    for cid, members in enumerate(partition):
        syms = []
        for m in members:
            name = g.vs[m]["name"]
            uri = g.vs[m]["uri"]
            if uri.startswith("file://"):
                uri = uri[7:]
            if root:
                try:
                    uri = str(Path(uri).relative_to(root.resolve()))
                except ValueError:
                    pass
            syms.append({
                "name": name,
                "kind": g.vs[m]["kind"],
                "file": uri,
            })
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