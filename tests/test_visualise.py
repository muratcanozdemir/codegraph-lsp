from __future__ import annotations

import importlib.util
import json
from pathlib import Path

_spec = importlib.util.spec_from_file_location(
    "visualise", Path(__file__).parent.parent / "scripts" / "visualise.py")
assert _spec and _spec.loader
vis = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(vis)


def _edge(s, t, w=1, typ="call", kind="function"):
    return {"source": s, "target": t, "source_id": s, "target_id": t,
            "source_kind": kind, "target_kind": kind, "weight": w, "type": typ}


def _report(tmp_path, edges, communities):
    d = {"symbol_graph": edges, "symbol_communities": [
        {"id": cid, "size": len(ids), "cohesion": 0,
         "symbols": [{"id": i, "name": i, "kind": "function", "file": vis.file_of(i)}
                     for i in ids]}
        for cid, ids in communities.items()]}
    p = tmp_path / "report.json"
    p.write_text(json.dumps(d))
    return str(p)


# star: every a.go symbol calls the hub, plus one link into b.go
def _star(tmp_path, n=6):
    leaves = [f"a.go::f{i}" for i in range(n)]
    edges = [_edge(x, "a.go::Hub") for x in leaves] + [_edge("a.go::f0", "b.go::g")]
    return _report(tmp_path, edges, {0: [*leaves, "a.go::Hub"], 1: ["b.go::g"]})


def _run(capsys, *argv):
    vis.main(list(argv))
    return capsys.readouterr().out


def test_auto_draws_symbols_when_small(tmp_path, capsys):
    out = _run(capsys, _star(tmp_path))
    assert '"a.go::f0"->"a.go::Hub"' in out


def test_auto_switches_to_communities_when_large(tmp_path, capsys):
    out = _run(capsys, _star(tmp_path), "--max-nodes", "3")
    assert '"c0"->"c1"' in out
    assert "a.go::f0" not in out


def test_hubs_are_left_out_and_listed(tmp_path, capsys):
    out = _run(capsys, _star(tmp_path), "--level", "symbol", "--hubs", "1")
    assert '->"a.go::Hub"' not in out
    assert "Hubs left out (1)" in out and "Hub  (6 dependents)" in out


def test_drill_down_keeps_members_and_stubs_other_communities(tmp_path, capsys):
    out = _run(capsys, _star(tmp_path), "--community", "0")
    assert '"a.go::f1"->"a.go::Hub"' in out
    assert '"b.go::g"[' not in out
    assert '"__self"->"__c1"[penwidth=1.0,color=gray,label=1' in out


def test_file_level_aggregates_and_disambiguates_basenames(tmp_path, capsys):
    edges = [_edge("x/u.go::a", "y/u.go::b", w=2), _edge("x/u.go::a", "y/u.go::c", w=3)]
    p = _report(tmp_path, edges, {0: ["x/u.go::a"], 1: ["y/u.go::b", "y/u.go::c"]})
    out = _run(capsys, p, "--level", "file")
    assert '"x/u.go"->"y/u.go"[penwidth=3.32]' in out
    assert 'label="x/u.go"' in out


def test_top_keeps_each_nodes_heaviest_links():
    from collections import Counter
    between = Counter({("a", "b"): 9, ("a", "c"): 1, ("c", "d"): 1})
    kept, hidden = vis.strongest(between, 1, 1)
    # a-c is neither a's nor (tied with c-d) c's top link; c-d is d's only link
    assert dict(kept) == {("a", "b"): 9, ("c", "d"): 1} and hidden == 1


def test_drilldowns_respect_auto_and_min_size(tmp_path, capsys):
    p = _star(tmp_path)
    assert _run(capsys, p, "--drilldowns") == ""
    assert _run(capsys, p, "--drilldowns", "--max-nodes", "3", "--min-size", "2") == "0\n"


def test_link_only_on_communities_with_pages(tmp_path, capsys):
    out = _run(capsys, _star(tmp_path), "--level", "community",
               "--link", "g-c{id}.svg", "--min-size", "2")
    assert 'URL="g-c0.svg"' in out and "g-c1.svg" not in out


def test_dark_fill_gets_white_text():
    assert vis.ink("#000075") == "#ffffff" and vis.ink("#bfef45") == "#000000"
