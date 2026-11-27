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
    (tmp_path / "a.py").write_text("x = 1\n")
    monkeypatch.setattr(sys, "argv", ["codegraph", str(tmp_path)])
    monkeypatch.setattr(cli.shutil, "which", lambda _binary: None)

    with pytest.raises(SystemExit) as exc:
        cli.main()

    assert exc.value.code == 1
    # names only the server the repo's languages need
    assert "no LSP servers found (need pyright-langserver)" in capsys.readouterr().err


def test_main_exits_when_there_is_no_source(tmp_path, monkeypatch, capsys):
    (tmp_path / "README.md").write_text("hi\n")
    monkeypatch.setattr(sys, "argv", ["codegraph", str(tmp_path)])

    with pytest.raises(SystemExit) as exc:
        cli.main()

    assert exc.value.code == 1
    assert "no source files (.py, .go)" in capsys.readouterr().err


class _Started(Exception):
    pass


def test_main_starts_only_servers_for_languages_present(tmp_path, monkeypatch):
    (tmp_path / "a.py").write_text("x = 1\n")
    (tmp_path / "vendor").mkdir()
    (tmp_path / "vendor" / "skipped.go").write_text("package v\n")
    monkeypatch.setattr(sys, "argv", ["codegraph", str(tmp_path)])
    monkeypatch.setattr(cli.shutil, "which", lambda binary: f"/usr/bin/{binary}")
    started = {}

    def fake_start(servers, root):
        started.update(servers)
        raise _Started

    monkeypatch.setattr(cli, "_start_clients", fake_start)

    with pytest.raises(_Started):
        cli.main()

    assert list(started) == ["python"]


def test_main_diff_needs_load_graph_before_starting_servers(tmp_path, monkeypatch, capsys):
    (tmp_path / "a.py").write_text("x = 1\n")
    monkeypatch.setattr(sys, "argv", ["codegraph", str(tmp_path), "--diff", "a.py"])
    monkeypatch.setattr(cli, "_start_clients", lambda *_: pytest.fail("servers started"))

    with pytest.raises(SystemExit) as exc:
        cli.main()

    assert exc.value.code == 1
    assert "--diff requires --load-graph" in capsys.readouterr().err


def test_find_servers_filters_to_available_binaries(monkeypatch):
    monkeypatch.setattr(cli.shutil, "which", lambda binary: "/usr/bin/pyright-langserver" if binary == "pyright-langserver" else None)

    servers = cli._find_servers()

    assert list(servers) == ["python"]


def test_find_servers_only_looks_for_the_languages_asked(monkeypatch):
    looked = []
    monkeypatch.setattr(cli.shutil, "which", lambda binary: looked.append(binary) or binary)

    assert list(cli._find_servers(["go"])) == ["go"]
    assert looked == ["gopls"]


def test_version_flag_prints_version(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["codegraph", "--version"])

    with pytest.raises(SystemExit) as exc:
        cli.main()

    assert exc.value.code == 0
    assert capsys.readouterr().out.startswith("codegraph ")


def test_jobs_must_be_positive(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["codegraph", str(tmp_path), "--jobs", "0"])

    with pytest.raises(SystemExit) as exc:
        cli.main()

    assert exc.value.code == 2
    assert "must be >= 1" in capsys.readouterr().err
