from __future__ import annotations

from pathlib import Path

from codegraph.extract import (
    Edge,
    Extractor,
    Symbol,
    SymbolGraph,
    resolve_edges,
    uri_to_path,
)


def _sym(name, qn, uri="file:///a.py", kind=12, line=0, character=0, language="python",
         end_line=-1):
    return Symbol(
        uri=uri, name=name, qualified_name=qn, kind=kind,
        line=line, character=character, language=language, end_line=end_line,
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


def test_symbol_graph_round_trip_keeps_same_named_symbols_apart():
    a = _sym("main", "main", uri="file:///a.go")
    b = _sym("main", "main", uri="file:///b.go")
    edge = Edge(source=a, target_uri="file:///c.go", target_line=1, edge_type="call")
    sg = SymbolGraph(symbols=[a, b], edges=[edge])

    restored = SymbolGraph.from_dict(sg.to_dict())

    assert len(restored.edges) == 1
    assert restored.edges[0].source.uri == "file:///a.go"


def test_symbol_graph_round_trip_preserves_per_symbol_language():
    sg = SymbolGraph(symbols=[
        _sym("f", "f", uri="file:///a.py", language="python"),
        _sym("g", "g", uri="file:///b.go", language="go"),
    ])

    restored = SymbolGraph.from_dict(sg.to_dict())

    assert [s.language for s in restored.symbols] == ["python", "go"]


def test_symbol_graph_from_dict_falls_back_to_language_argument():
    data = {"symbols": [{"uri": "file:///a.py", "name": "f", "qualified_name": "f",
                         "kind": 12, "line": 0}]}

    restored = SymbolGraph.from_dict(data, language="python")

    assert restored.symbols[0].language == "python"


def test_resolve_edges_maps_to_nearest_enclosing_symbol():
    a = _sym("a", "a", uri="file:///a.py", kind=12, line=0)
    b = _sym("b", "b", uri="file:///b.py", kind=12, line=0)
    c = _sym("c", "c", uri="file:///b.py", kind=12, line=10)

    edge_to_b = Edge(source=a, target_uri="file:///b.py", target_line=3, edge_type="reference")
    edge_to_c = Edge(source=a, target_uri="file:///b.py", target_line=15, edge_type="reference")
    sg = SymbolGraph(symbols=[a, b, c], edges=[edge_to_b, edge_to_c])

    resolved = resolve_edges(sg)

    # reference edges point use site -> definition
    assert (b, a, "reference") in resolved
    assert (c, a, "reference") in resolved


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


def test_resolve_edges_drops_module_level_code_after_a_function():
    caller = _sym("g", "g", uri="file:///x.py", line=0, end_line=4)
    f = _sym("f", "f", uri="file:///m.py", line=0, end_line=10)
    edge = Edge(source=caller, target_uri="file:///m.py", target_line=500, edge_type="call")

    assert resolve_edges(SymbolGraph([caller, f], [edge])) == []


def test_resolve_edges_picks_innermost_containing_symbol():
    caller = _sym("g", "g", uri="file:///x.py", line=0, end_line=4)
    cls = _sym("C", "C", uri="file:///m.py", kind=5, line=0, end_line=30)
    meth = _sym("m", "C.m", uri="file:///m.py", kind=6, line=2, end_line=5)
    edges = [
        Edge(source=caller, target_uri="file:///m.py", target_line=3, edge_type="call"),
        # class body after the method: belongs to the class, not the method
        Edge(source=caller, target_uri="file:///m.py", target_line=20, edge_type="call"),
    ]

    resolved = resolve_edges(SymbolGraph([caller, cls, meth], edges))

    assert resolved == [(caller, meth, "call"), (caller, cls, "call")]


def test_resolve_edges_without_extents_falls_back_to_nearest_preceding_symbol():
    # graphs saved before end_line existed
    caller = _sym("g", "g", uri="file:///x.py")
    f = _sym("f", "f", uri="file:///m.py", line=0)
    edge = Edge(source=caller, target_uri="file:///m.py", target_line=500, edge_type="call")

    assert resolve_edges(SymbolGraph([caller, f], [edge])) == [(caller, f, "call")]


def test_flatten_symbols_records_full_range_end():
    ex = Extractor(clients={}, root=Path("/"))
    items = [{
        "name": "f", "kind": 12,
        "range": {"start": {"line": 1, "character": 0}, "end": {"line": 9, "character": 0}},
        "selectionRange": {"start": {"line": 2, "character": 4},
                           "end": {"line": 2, "character": 5}},
    }]

    (sym,) = ex._flatten_symbols(items, "file:///a.py", "python")

    assert (sym.line, sym.end_line) == (2, 9)


def test_symbol_graph_round_trip_preserves_end_line():
    sg = SymbolGraph(symbols=[_sym("f", "f", end_line=12)])

    assert SymbolGraph.from_dict(sg.to_dict()).symbols[0].end_line == 12


def test_uri_to_path_percent_decodes():
    assert uri_to_path(Path("/tmp/my repo/a#b.py").as_uri()) == "/tmp/my repo/a#b.py"
    assert uri_to_path("/already/a/path.py") == "/already/a/path.py"


def test_discover_prunes_skipped_dirs_and_generated_files(tmp_path):
    for rel in ["a.go", "a.pb.go", "a_test.go", "pkg/b.go",
                "vendor/x/c.go", "node_modules/d.go", ".git/e.go"]:
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("package x\n")
    ex = Extractor(clients={}, root=tmp_path)

    found = [p.relative_to(tmp_path).as_posix() for p in ex._discover("go")]

    assert found == ["a.go", "pkg/b.go"]


def _touch(root, rels):
    for rel in rels:
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("")


_PY_TREE = ["src/pkg/core.py", "src/pkg/testing_utils.py", "src/pkg/test_core.py",
            "src/pkg/core_test.py", "conftest.py", "tests/fake_server.py",
            "pkg/testdata/sample.py", "examples/demo.py"]


def test_discover_skips_test_code_by_default(tmp_path):
    _touch(tmp_path, _PY_TREE)

    found = [p.relative_to(tmp_path).as_posix() for p in Extractor({}, tmp_path)._discover("python")]

    assert found == ["examples/demo.py", "src/pkg/core.py", "src/pkg/testing_utils.py"]


def test_discover_include_tests_keeps_everything(tmp_path):
    _touch(tmp_path, _PY_TREE)

    ex = Extractor({}, tmp_path, include_tests=True)

    assert len(list(ex._discover("python"))) == len(_PY_TREE)


def test_discover_exclude_globs_match_path_or_component(tmp_path):
    _touch(tmp_path, _PY_TREE)

    ex = Extractor({}, tmp_path, exclude=["examples", "src/*/testing_*"])
    found = [p.relative_to(tmp_path).as_posix() for p in ex._discover("python")]

    assert found == ["src/pkg/core.py"]


def test_extract_explicit_files_apply_the_same_filter(tmp_path):
    _touch(tmp_path, ["src/a.py", "tests/test_a.py"])
    client = _FakeClient(n_symbols=1, delay=0)

    sg = Extractor({"python": client}, tmp_path).extract(
        files=[tmp_path / "src/a.py", tmp_path / "tests/test_a.py"])

    assert {s.file_path for s in sg.symbols} == {str((tmp_path / "src/a.py").resolve())}


class _FakeClient:
    """Answers every lookup; records peak concurrency."""

    def __init__(self, n_symbols: int, delay: float):
        import threading

        from codegraph.lsp import ServerCapabilities

        self.capabilities = ServerCapabilities(references=True, call_hierarchy=False)
        self._n, self._delay = n_symbols, delay
        self._lock, self.active, self.peak = threading.Lock(), 0, 0

    def open_document(self, uri, text):
        pass

    def close_document(self, uri):
        pass

    def document_symbols(self, uri):
        return [{
            "name": f"f{i}", "kind": 12,
            "range": {"start": {"line": i, "character": 0}, "end": {"line": i, "character": 9}},
            "selectionRange": {"start": {"line": i, "character": 4},
                               "end": {"line": i, "character": 6}},
        } for i in range(self._n)]

    def references(self, uri, line, character):
        import time

        with self._lock:
            self.active += 1
            self.peak = max(self.peak, self.active)
        time.sleep(self._delay)
        with self._lock:
            self.active -= 1
        return [{"uri": "file:///other.py", "range": {"start": {"line": line}}}]


def test_extract_runs_lookups_concurrently_and_keeps_edge_order(tmp_path):
    f = tmp_path / "a.py"
    f.write_text("")
    client = _FakeClient(n_symbols=8, delay=0.05)

    sg = Extractor({"python": client}, tmp_path, jobs=4).extract(files=[f])

    assert client.peak > 1
    assert [e.source.name for e in sg.edges] == [f"f{i}" for i in range(8)]


def test_extract_with_one_job_is_sequential(tmp_path):
    f = tmp_path / "a.py"
    f.write_text("")
    client = _FakeClient(n_symbols=4, delay=0.01)

    Extractor({"python": client}, tmp_path, jobs=1).extract(files=[f])

    assert client.peak == 1
