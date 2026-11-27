"""Render a codegraph report as Graphviz dot on stdout.

Usage:
    python3 scripts/visualise.py report.json | sfdp -Tsvg -o codegraph.svg
    python3 scripts/visualise.py report.json --level community   # one node per community
    python3 scripts/visualise.py report.json --level file        # one node per file
    python3 scripts/visualise.py report.json --community 3 --hubs 5   # drill into one
    python3 scripts/visualise.py report.json --drilldowns        # ids worth drilling into

Levels:
    symbol     every function/class/struct (edges with weight >= 2, or calls)
    file       files, coloured by the symbol community most of their symbols are in
    community  symbol communities, edges weighted by the links between them
    auto       symbol if that draws at most --max-nodes nodes, community otherwise
"""

from __future__ import annotations

import argparse
import json
import math
from collections import Counter, defaultdict

COLORS = ["#e6194b", "#3cb44b", "#4363d8", "#f58231", "#911eb4", "#42d4f4",
          "#f032e6", "#bfef45", "#fabed4", "#469990", "#dcbeff", "#9A6324",
          "#800000", "#aaffc3", "#808000", "#000075"]

SHAPES = {"function": "ellipse", "method": "ellipse", "class": "box",
          "struct": "box3d", "interface": "diamond", "constructor": "hexagon"}

STUB = "#f0f0f0"


def quote(s: str) -> str:
    """dot double-quoted string literal."""
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


# Reports from before symbol ids existed only carry qualified names.
def src(e: dict) -> str:
    return e.get("source_id", e["source"])


def tgt(e: dict) -> str:
    return e.get("target_id", e["target"])


def file_of(sym_id: str) -> str:
    return sym_id.rsplit("::", 1)[0] if "::" in sym_id else sym_id


def color(cid: int | None) -> str:
    return STUB if cid is None else COLORS[cid % len(COLORS)]


def ink(fill: str) -> str:
    """Text colour that stays readable on this fill."""
    r, g, b = (int(fill[i:i + 2], 16) for i in (1, 3, 5))
    return "#ffffff" if 0.299 * r + 0.587 * g + 0.114 * b < 128 else "#000000"


def strongest(between: Counter, k: int, min_weight: int) -> tuple[list, int]:
    """Aggregated edges at or above min_weight that are among the k heaviest of
    either endpoint (k <= 0 keeps them all). Returns (kept, number hidden)."""
    heavy = [(pair, w) for pair, w in between.items() if w >= min_weight]
    if k > 0:
        ranked: dict = defaultdict(list)
        for pair, w in heavy:
            for end in pair:
                ranked[end].append((w, pair))
        keep = {pair for lst in ranked.values() for _, pair in sorted(lst, reverse=True)[:k]}
        heavy = [(pair, w) for pair, w in heavy if pair in keep]
    return sorted(heavy), len(between) - len(heavy)


def caption(out: list[str], text: str) -> None:
    out.append(f"label={quote(text)};labelloc=b;fontsize=10;")


def pen(w: float) -> float:
    """Edge width for an aggregated weight: logarithmic, 1..8."""
    return round(min(1 + math.log2(max(w, 1)), 8), 2)


class Report:
    def __init__(self, d: dict):
        self.edges: list[dict] = d.get("symbol_graph", [])
        self.communities: list[dict] = d.get("symbol_communities", [])
        self.comm_of: dict[str, int] = {}
        for c in self.communities:
            for s in c["symbols"]:
                self.comm_of[s.get("id", s["name"])] = c["id"]

    def strong_edges(self) -> list[dict]:
        """The edges the symbol view draws: repeated references, or any call."""
        return [e for e in self.edges if e["weight"] >= 2 or e["type"] == "call"]

    def symbol_node_count(self) -> int:
        return len({n for e in self.strong_edges() for n in (src(e), tgt(e))})

    def community(self, cid: int) -> dict:
        for c in self.communities:
            if c["id"] == cid:
                return c
        raise SystemExit(f"no symbol community {cid} in report "
                         f"(have {sorted(c['id'] for c in self.communities)})")


def hubs(edges: list[dict], k: int) -> list[tuple[str, int]]:
    """The k symbols with the most distinct dependents among these edges."""
    if k <= 0:
        return []
    dependents: dict[str, set[str]] = defaultdict(set)
    for e in edges:
        dependents[tgt(e)].add(src(e))
    ranked = sorted(dependents.items(), key=lambda kv: (-len(kv[1]), kv[0]))
    return [(n, len(deps)) for n, deps in ranked[:k]]


def short(sym_id: str) -> str:
    return sym_id.rsplit("::", 1)[-1].split(".")[-1]


def legend(title: str, rows: list[str]) -> str:
    """A plaintext node listing rows under a title."""
    cells = "".join(f'<tr><td align="left">{esc_html(r)}</td></tr>' for r in rows)
    return (f'"__legend"[shape=plaintext,fillcolor="#ffffff",label=<'
            f'<table border="1" cellborder="0" cellspacing="0" cellpadding="3">'
            f'<tr><td align="left"><b>{esc_html(title)}</b></td></tr>{cells}</table>>];')


def esc_html(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def header(out: list[str], fontsize: int = 8, splines: str = "curved") -> None:
    # splines=true routes edges around nodes: fine for a few dozen nodes, but it
    # turns a 300-symbol layout from 0.1s into 20s and the full graph into minutes.
    out.append(f"digraph{{overlap=prism;splines={splines};rankdir=LR;")
    out.append(f'node[style=filled,fontsize={fontsize},fontname="Helvetica"];')


def render_symbols(r: Report, edges: list[dict], n_hubs: int, out: list[str],
                   keep: set[str] | None = None) -> None:
    """Symbol nodes and edges; hubs are dropped and listed in a legend."""
    dropped = hubs(edges, n_hubs)
    hub_ids = {n for n, _ in dropped}
    edges = [e for e in edges if src(e) not in hub_ids and tgt(e) not in hub_ids]

    # node -> kind, from whichever surviving edge mentions it first
    kinds: dict[str, str] = {}
    for e in edges:
        for node, kind in ((src(e), e["source_kind"]), (tgt(e), e["target_kind"])):
            if keep is None or node in keep:
                kinds.setdefault(node, kind)

    for node, kind in kinds.items():
        shape = SHAPES.get(kind, "ellipse")
        out.append(f"{quote(node)}[label={quote(short(node))},shape={shape},"
                   f"fillcolor={quote(color(r.comm_of.get(node)))},"
                   f"fontcolor={quote(ink(color(r.comm_of.get(node))))}];")
    for e in edges:
        if src(e) in kinds and tgt(e) in kinds:
            style = "dashed" if e["type"] == "reference" else "solid"
            out.append(f"{quote(src(e))}->{quote(tgt(e))}"
                       f"[penwidth={min(e['weight'], 6)},style={style}];")
    if dropped:
        out.append(legend(f"Hubs left out ({len(dropped)})",
                          [f"{short(n)}  ({n_deps} dependents)  {file_of(n)}"
                           for n, n_deps in dropped]))


def level_symbol(r: Report, n_hubs: int) -> list[str]:
    out: list[str] = []
    header(out)
    render_symbols(r, r.strong_edges(), n_hubs, out)
    out.append("}")
    return out


def level_drill(r: Report, cid: int, n_hubs: int) -> list[str]:
    """One community's symbols, with a stub node per community it links to."""
    c = r.community(cid)
    members = {s.get("id", s["name"]) for s in c["symbols"]}
    out: list[str] = []
    header(out)
    out.append(f'label={quote(f"Community {cid}: {c["size"]} symbols")};labelloc=t;fontsize=14;')
    internal = [e for e in r.strong_edges() if src(e) in members and tgt(e) in members]
    render_symbols(r, internal, n_hubs, out, keep=members)

    # links leaving or entering the community, collapsed per other community
    outgoing: Counter[int] = Counter()
    incoming: Counter[int] = Counter()
    for e in r.edges:
        s_in, t_in = src(e) in members, tgt(e) in members
        if s_in and not t_in and tgt(e) in r.comm_of:
            outgoing[r.comm_of[tgt(e)]] += e["weight"]
        elif t_in and not s_in and src(e) in r.comm_of:
            incoming[r.comm_of[src(e)]] += e["weight"]
    for other in sorted(set(outgoing) | set(incoming)):
        sid = f"__c{other}"
        out.append(f"{quote(sid)}[label={quote(f'community {other}')},shape=folder,"
                   f"fillcolor={quote(color(other))},fontcolor={quote(ink(color(other)))},"
                   f"style=\"filled,dashed\"];")
        anchor = '"__self"'
        if outgoing[other]:
            out.append(f"{anchor}->{quote(sid)}[penwidth={pen(outgoing[other])},"
                       f"color=gray,label={outgoing[other]},fontsize=7];")
        if incoming[other]:
            out.append(f"{quote(sid)}->{anchor}[penwidth={pen(incoming[other])},"
                       f"color=gray,label={incoming[other]},fontsize=7];")
    if outgoing or incoming:
        out.append(f'"__self"[label={quote(f"community {cid}")},shape=folder,'
                   f"fillcolor={quote(color(cid))},fontcolor={quote(ink(color(cid)))}];")
    out.append("}")
    return out


def top_files(c: dict, n: int = 3) -> list[str]:
    counts = Counter(s["file"] for s in c["symbols"])
    files = [f for f, _ in counts.most_common(n)]
    if len(counts) > n:
        files.append(f"+{len(counts) - n} more files")
    return files


def level_community(r: Report, min_weight: int, top: int, link: str | None,
                    min_size: int) -> list[str]:
    out: list[str] = []
    header(out, fontsize=10, splines="true")
    out.append('edge[dir=none,color="#00000066"];')
    largest = max((c["size"] for c in r.communities), default=1)
    for c in r.communities:
        label = "\\n".join([f"C{c['id']}: {c['size']} symbols", *top_files(c)])
        fs = round(9 + 7 * math.sqrt(c["size"] / largest), 1)
        url = f",URL={quote(link.format(id=c['id']))}" if link and c["size"] >= min_size else ""
        # label is already dot-escaped (\n), so quote it by hand
        out.append(f'"c{c["id"]}"[label="{label.replace(chr(34), "")}",shape=box,'
                   f'style="filled,rounded",fontsize={fs},'
                   f"fillcolor={quote(color(c['id']))},fontcolor={quote(ink(color(c['id'])))}"
                   f"{url}];")
    between: Counter[tuple[int, int]] = Counter()
    for e in r.edges:
        a, b = r.comm_of.get(src(e)), r.comm_of.get(tgt(e))
        if a is not None and b is not None and a != b:
            between[(min(a, b), max(a, b))] += e["weight"]
    kept, hidden = strongest(between, top, min_weight)
    for (a, b), w in kept:
        out.append(f'"c{a}"->"c{b}"[penwidth={pen(w)},label={w},fontsize=8];')
    if hidden:
        caption(out, f"{hidden} weaker links between communities not drawn")
    out.append("}")
    return out


def file_labels(files: list[str]) -> dict[str, str]:
    """Basename, or enough trailing path to tell files with the same basename apart."""
    labels = {}
    for f in files:
        parts = f.split("/")
        n = 1
        while n < len(parts) and sum(
            1 for g in files if g.split("/")[-n:] == parts[-n:]
        ) > 1:
            n += 1
        labels[f] = "/".join(parts[-n:])
    return labels


def level_file(r: Report, min_weight: int, top: int) -> list[str]:
    # colour each file by the community most of its symbols belong to
    votes: dict[str, Counter[int]] = defaultdict(Counter)
    for sym, cid in r.comm_of.items():
        votes[file_of(sym)][cid] += 1
    out: list[str] = []
    header(out, fontsize=9, splines="true")
    out.append('edge[dir=none,color="#00000066"];')
    labels = file_labels(sorted(votes))
    for f in sorted(votes):
        fill = color(votes[f].most_common(1)[0][0])
        out.append(f"{quote(f)}[label={quote(labels[f])},tooltip={quote(f)},"
                   f"shape=note,fillcolor={quote(fill)},fontcolor={quote(ink(fill))}];")
    between: Counter[tuple[str, str]] = Counter()
    for e in r.edges:
        a, b = file_of(src(e)), file_of(tgt(e))
        if a != b:
            between[(min(a, b), max(a, b))] += e["weight"]
    kept, hidden = strongest(between, top, min_weight)
    for (a, b), w in kept:
        out.append(f"{quote(a)}->{quote(b)}[penwidth={pen(w)}];")
    if hidden:
        caption(out, f"{hidden} weaker file links not drawn")
    out.append("}")
    return out


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("report", help="codegraph report JSON (-o output)")
    p.add_argument("--level", choices=["auto", "symbol", "file", "community"],
                   default="auto", help="what one node stands for (default: auto)")
    p.add_argument("--community", type=int, metavar="ID",
                   help="draw only this symbol community's symbols")
    p.add_argument("--hubs", type=int, default=0, metavar="K",
                   help="leave out the K symbols with the most dependents, listing them "
                        "in a legend (symbol and --community views)")
    p.add_argument("--max-nodes", type=int, default=300,
                   help="largest symbol graph --level auto draws as is (default: 300)")
    p.add_argument("--min-weight", type=int, default=1,
                   help="hide aggregated edges lighter than this (file/community views)")
    p.add_argument("--top", type=int, default=3, metavar="K",
                   help="file/community views: draw a link only if it is among the K "
                        "heaviest of one of its ends (default: 3; 0 = all)")
    p.add_argument("--link", metavar="PATTERN",
                   help="community view: link each node of at least --min-size symbols to "
                        "PATTERN, {id} replaced by its id, e.g. codegraph-c{id}.svg")
    p.add_argument("--drilldowns", action="store_true",
                   help="print the ids of communities worth a --community view, one per "
                        "line; nothing when --level auto would draw symbols")
    p.add_argument("--min-size", type=int, default=10,
                   help="smallest community --drilldowns lists (default: 10)")
    args = p.parse_args(argv)

    with open(args.report) as f:
        r = Report(json.load(f))

    level = args.level
    if level == "auto":
        level = "symbol" if r.symbol_node_count() <= args.max_nodes else "community"

    if args.drilldowns:
        if args.level == "auto" and level == "symbol":
            return
        for c in r.communities:
            if c["size"] >= args.min_size:
                print(c["id"])
        return

    if args.community is not None:
        lines = level_drill(r, args.community, args.hubs)
    elif level == "symbol":
        lines = level_symbol(r, args.hubs)
    elif level == "file":
        lines = level_file(r, args.min_weight, args.top)
    else:
        lines = level_community(r, args.min_weight, args.top, args.link, args.min_size)
    print("\n".join(lines))


if __name__ == "__main__":
    main()
