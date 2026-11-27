"""Render a codegraph report's symbol graph as Graphviz dot on stdout.

Usage: python3 scripts/visualise.py report.json | sfdp -Tsvg -o codegraph.svg
"""

import json
import sys

COLORS = ["#e6194b", "#3cb44b", "#4363d8", "#f58231", "#911eb4", "#42d4f4",
          "#f032e6", "#bfef45", "#fabed4", "#469990", "#dcbeff", "#9A6324",
          "#800000", "#aaffc3", "#808000", "#000075"]

SHAPES = {"function": "ellipse", "method": "ellipse", "class": "box",
          "struct": "box3d", "interface": "diamond", "constructor": "hexagon"}


def quote(s: str) -> str:
    """dot double-quoted string literal."""
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def main(path: str) -> None:
    with open(path) as f:
        d = json.load(f)

    # Reports from before symbol ids existed only carry qualified names.
    def src(e):
        return e.get("source_id", e["source"])

    def tgt(e):
        return e.get("target_id", e["target"])

    sym_color = {}
    for c in d.get("symbol_communities", []):
        for s in c["symbols"]:
            sym_color[s.get("id", s["name"])] = COLORS[c["id"] % len(COLORS)]

    # filter: only edges with weight >= 2 or type == "call"
    edges = [e for e in d.get("symbol_graph", []) if e["weight"] >= 2 or e["type"] == "call"]

    # node -> kind, from whichever surviving edge mentions it first
    kinds: dict[str, str] = {}
    for e in edges:
        kinds.setdefault(src(e), e["source_kind"])
        kinds.setdefault(tgt(e), e["target_kind"])

    print("digraph{overlap=prism;splines=true;rankdir=LR;")
    print('node[style=filled,fontsize=8,fontname="Helvetica"];')
    for node, kind in kinds.items():
        short = node.rsplit("::", 1)[-1].split(".")[-1]
        shape = SHAPES.get(kind, "ellipse")
        fill = sym_color.get(node, "#f0f0f0")
        print(f"{quote(node)}[label={quote(short)},shape={shape},fillcolor={quote(fill)}];")
    for e in edges:
        pw = min(e["weight"], 6)
        style = "dashed" if e["type"] == "reference" else "solid"
        print(f"{quote(src(e))}->{quote(tgt(e))}[penwidth={pw},style={style}];")
    print("}")


if __name__ == "__main__":
    main(sys.argv[1])
