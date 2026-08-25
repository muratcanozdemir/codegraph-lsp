from __future__ import annotations

import sys

import pytest

from codegraph import __main__ as cli
from codegraph.extract import Edge, Symbol, SymbolGraph


def _sym(name, uri, line=0):
    return Symbol(uri=uri, name=name, qualified_name=name, kind=12, line=line, character=0, language="python")


def test_merge_incremental_replaces_symbols_for_changed_files():
    kept = _sym("kept", "file:///a.py")
    stale = _sym("stale", "file:///b.py")
    fresh = _sym("fresh", "file:///b.py")
    base = SymbolGraph(symbols=[kept, stale], edges=[])
    delta = SymbolGraph(symbols=[fresh], edges=[])

    merged = cli._merge_incremental(base, delta, changed_uris={"file:///b.py"})

    assert merged.symbols == [kept, fresh]


def test_merge_incremental_drops_stale_outgoing_edges_from_changed_files():
    changed_src = _sym("c", "file:///b.py")
    edge_from_changed = Edge(source=changed_src, target_uri="file:///a.py", target_line=0, edge_type="reference")
    base = SymbolGraph(symbols=[changed_src], edges=[edge_from_changed])
    delta = SymbolGraph(symbols=[], edges=[])

    merged = cli._merge_incremental(base, delta, changed_uris={"file:///b.py"})

    assert merged.edges == []


def test_merge_incremental_keeps_incoming_edges_into_changed_files():
    # Regression test: an edge sourced from an *unchanged* file that targets
    # a changed file must survive the merge — delta extraction never
    # re-visits unchanged files, so dropping this edge would silently lose
    # real coupling data with no way to recover it.
    unchanged_src = _sym("a", "file:///a.py")
    edge_into_changed = Edge(source=unchanged_src, target_uri="file:///b.py", target_line=0, edge_type="reference")
    base = SymbolGraph(symbols=[unchanged_src], edges=[edge_into_changed])
    delta = SymbolGraph(symbols=[], edges=[])

    merged = cli._merge_incremental(base, delta, changed_uris={"file:///b.py"})

    assert merged.edges == [edge_into_changed]


def test_main_exits_when_root_is_not_a_directory(tmp_path, monkeypatch, capsys):
    missing = tmp_path / "does-not-exist"
    monkeypatch.setattr(sys, "argv", ["codegraph", str(missing)])

    with pytest.raises(SystemExit) as exc:
        cli.main()

    assert exc.value.code == 1
    assert "not a directory" in capsys.readouterr().err


def test_main_exits_when_no_lsp_servers_available(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["codegraph", str(tmp_path)])
    monkeypatch.setattr(cli.shutil, "which", lambda _binary: None)

    with pytest.raises(SystemExit) as exc:
        cli.main()

    assert exc.value.code == 1
    assert "no LSP servers found" in capsys.readouterr().err


def test_find_servers_filters_to_available_binaries(monkeypatch):
    monkeypatch.setattr(cli.shutil, "which", lambda binary: "/usr/bin/pyright-langserver" if binary == "pyright-langserver" else None)

    servers = cli._find_servers()

    assert list(servers) == ["python"]
