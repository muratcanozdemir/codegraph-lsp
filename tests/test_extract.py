from __future__ import annotations

from codegraph.extract import Edge, Extractor, Symbol, SymbolGraph, resolve_edges


def _sym(name, qn, uri="file:///a.py", kind=12, line=0, character=0, language="python"):
    return Symbol(
        uri=uri, name=name, qualified_name=qn, kind=kind,
        line=line, character=character, language=language,
    )


def test_symbol_file_path_strips_file_prefix():
    sym = _sym("f", "f", uri="file:///repo/a.py")
    assert sym.file_path == "/repo/a.py"


def test_symbol_kind_name_known_and_unknown():
    assert _sym("f", "f", kind=12).kind_name == "function"
    assert _sym("f", "f", kind=999).kind_name == "unknown"


def test_flatten_symbols_hierarchical_qualified_names():
    extractor = Extractor(clients={}, root=__import__("pathlib").Path("."))
    hierarchical = [
        {
            "name": "Foo",
            "kind": 5,
            "selectionRange": {"start": {"line": 0, "character": 0}},
            "children": [
                {
                    "name": "bar",
                    "kind": 6,
                    "selectionRange": {"start": {"line": 1, "character": 4}},
                },
            ],
        },
    ]
    symbols = list(extractor._flatten_symbols(hierarchical, "file:///a.py", "python"))
    names = {s.qualified_name: s for s in symbols}
    assert set(names) == {"Foo", "Foo.bar"}
    assert names["Foo.bar"].line == 1
    assert names["Foo.bar"].kind == 6


def test_flatten_symbols_flat_symbol_information_uses_location_uri():
    extractor = Extractor(clients={}, root=__import__("pathlib").Path("."))
    flat = [
        {
            "name": "Baz",
            "kind": 12,
            "location": {
                "uri": "file:///other.py",
                "range": {"start": {"line": 3, "character": 0}},
            },
        },
    ]
    symbols = list(extractor._flatten_symbols(flat, "file:///a.py", "python"))
    assert len(symbols) == 1
    assert symbols[0].uri == "file:///other.py"
    assert symbols[0].line == 3


def test_symbol_graph_round_trip_preserves_kind_and_character():
    sym = _sym("f", "f", kind=12, line=5, character=7)
    sg = SymbolGraph(symbols=[sym], edges=[])

    restored = SymbolGraph.from_dict(sg.to_dict(), language="python")

    assert len(restored.symbols) == 1
    got = restored.symbols[0]
    assert got.kind == 12
    assert got.kind_name == "function"
    assert got.character == 7


def test_symbol_graph_round_trip_preserves_edges_by_qualified_name():
    src = _sym("f", "f", kind=12)
    edge = Edge(source=src, target_uri="file:///b.py", target_line=2, edge_type="call")
    sg = SymbolGraph(symbols=[src], edges=[edge])

    restored = SymbolGraph.from_dict(sg.to_dict(), language="python")

    assert len(restored.edges) == 1
    assert restored.edges[0].target_uri == "file:///b.py"
    assert restored.edges[0].edge_type == "call"
    assert restored.edges[0].source.qualified_name == "f"


def test_resolve_edges_maps_to_nearest_enclosing_symbol():
    a = _sym("a", "a", uri="file:///a.py", kind=12, line=0)
    b = _sym("b", "b", uri="file:///b.py", kind=12, line=0)
    c = _sym("c", "c", uri="file:///b.py", kind=12, line=10)

    edge_to_b = Edge(source=a, target_uri="file:///b.py", target_line=3, edge_type="reference")
    edge_to_c = Edge(source=a, target_uri="file:///b.py", target_line=15, edge_type="reference")
    sg = SymbolGraph(symbols=[a, b, c], edges=[edge_to_b, edge_to_c])

    resolved = resolve_edges(sg)

    assert (a, b, "reference") in resolved
    assert (a, c, "reference") in resolved


def test_resolve_edges_ignores_self_references():
    a = _sym("a", "a", uri="file:///a.py", kind=12, line=0)
    edge = Edge(source=a, target_uri="file:///a.py", target_line=0, edge_type="reference")
    sg = SymbolGraph(symbols=[a], edges=[edge])

    assert resolve_edges(sg) == []


def test_resolve_edges_excludes_uninteresting_symbol_kinds():
    # kind=13 is "variable" — not in INTERESTING_KINDS, should never be a
    # resolution target even though it is the nearest preceding symbol.
    caller = _sym("caller", "caller", uri="file:///a.py", kind=12, line=0)
    variable = _sym("v", "v", uri="file:///b.py", kind=13, line=0)
    edge = Edge(source=caller, target_uri="file:///b.py", target_line=1, edge_type="reference")
    sg = SymbolGraph(symbols=[caller, variable], edges=[edge])

    assert resolve_edges(sg) == []
