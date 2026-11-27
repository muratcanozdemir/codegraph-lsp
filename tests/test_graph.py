from __future__ import annotations

import json
from pathlib import Path

from codegraph.extract import Edge, Symbol, SymbolGraph
from codegraph.graph import (
    analyze,
    analyze_symbols,
    build_file_graph,
    build_symbol_graph,
    save_report,
)


def _sym(name, uri, kind=12, line=0, language="python"):
    return Symbol(
        uri=uri, name=name, qualified_name=name, kind=kind,
        line=line, character=0, language=language,
    )


def _two_file_graph():
    a = _sym("a", "file:///a.py", line=0)
    b = _sym("b", "file:///b.py", line=0)
    edge = Edge(source=a, target_uri="file:///b.py", target_line=0, edge_type="reference")
    return SymbolGraph(symbols=[a, b], edges=[edge])


def test_build_file_graph_empty_graph_has_no_vertices():
    g = build_file_graph(SymbolGraph())
    assert g.vcount() == 0


def test_build_file_graph_collapses_cross_file_edges():
    sg = _two_file_graph()
    g = build_file_graph(sg)
    assert g.vcount() == 2
    assert g.ecount() == 1
    assert list(g.es["weight"]) == [1]


def test_build_file_graph_ignores_same_file_edges():
    a = _sym("a", "file:///a.py", line=0)
    b = _sym("b", "file:///a.py", line=1)
    edge = Edge(source=a, target_uri="file:///a.py", target_line=1, edge_type="reference")
    sg = SymbolGraph(symbols=[a, b], edges=[edge])

    g = build_file_graph(sg)
    assert g.vcount() == 1
    assert g.ecount() == 0


def test_build_symbol_graph_resolves_and_weights_edges():
    sg = _two_file_graph()
    g = build_symbol_graph(sg)
    assert g.vcount() == 2
    assert g.ecount() == 1
    assert g.es["type"][0] == "reference"


def test_analyze_on_empty_graph_returns_zeroed_result():
    result = analyze(SymbolGraph())
    assert result.node_count == 0
    assert result.edge_count == 0
    assert result.communities == []
    assert result.modularity == 0.0


def test_analyze_two_coupled_files_forms_single_community():
    sg = _two_file_graph()
    result = analyze(sg)
    assert result.node_count == 2
    assert result.edge_count == 1
    assert len(result.communities) == 1
    assert sorted(result.communities[0].files) == ["file:///a.py", "file:///b.py"]
    assert result.communities[0].languages == ["python"]


def test_analyze_result_to_dict_round_trips_through_json():
    sg = _two_file_graph()
    result = analyze(sg)
    data = json.loads(json.dumps(result.to_dict()))
    assert data["summary"]["files"] == 2
    assert data["summary"]["edges"] == 1
    assert len(data["communities"]) == 1


def test_save_report_writes_json(tmp_path: Path):
    sg = _two_file_graph()
    result = analyze(sg)
    out = tmp_path / "report.json"

    save_report(result, out)

    data = json.loads(out.read_text())
    assert data["summary"]["files"] == 2


def test_analyze_symbols_empty_graph_returns_no_communities():
    assert analyze_symbols(SymbolGraph()) == []


def test_analyze_symbols_groups_coupled_symbols_together():
    sg = _two_file_graph()
    communities = analyze_symbols(sg)
    assert len(communities) == 1
    names = {s["name"] for s in communities[0]["symbols"]}
    assert names == {"a", "b"}
    assert communities[0]["cohesion"] > 0


def test_symbol_graph_edges_carry_unique_ids_for_same_named_symbols(tmp_path):
    root = tmp_path
    a = Symbol(uri=(root / "a.go").as_uri(), name="main", qualified_name="main",
               kind=12, line=0, character=0, language="go")
    b = Symbol(uri=(root / "b.go").as_uri(), name="main", qualified_name="main",
               kind=12, line=0, character=0, language="go")
    sg = SymbolGraph([a, b], [Edge(a, b.uri, 0, "call")])

    result = analyze(sg, root=root)

    (edge,) = result.symbol_graph
    assert (edge["source_id"], edge["target_id"]) == ("a.go::main", "b.go::main")
    ids = {s["id"] for c in result.symbol_communities for s in c["symbols"]}
    assert ids == {"a.go::main", "b.go::main"}


def test_reported_paths_are_percent_decoded_and_root_relative(tmp_path):
    root = tmp_path / "my repo"
    a = Symbol(uri=(root / "a.py").as_uri(), name="a", qualified_name="a",
               kind=12, line=0, character=0, language="python")
    b = Symbol(uri=(root / "b.py").as_uri(), name="b", qualified_name="b",
               kind=12, line=0, character=0, language="python")
    sg = SymbolGraph([a, b], [Edge(a, b.uri, 0, "call")])

    result = analyze(sg, root=root)

    files = {s["file"] for c in result.symbol_communities for s in c["symbols"]}
    assert files == {"a.py", "b.py"}


def test_symbol_graph_prefers_call_type_when_call_and_reference_coincide():
    caller = _sym("caller", "file:///a.py")
    callee = _sym("callee", "file:///b.py")
    sg = SymbolGraph([caller, callee], [
        Edge(caller, "file:///b.py", 0, "call"),        # caller -> callee
        Edge(callee, "file:///a.py", 0, "reference"),   # callee used in caller
    ])

    g = build_symbol_graph(sg)

    assert g.ecount() == 1
    assert g.es[0]["type"] == "call"
    assert g.es[0]["weight"] == 2
