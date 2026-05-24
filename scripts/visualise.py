import json, sys, os

d = json.load(open(sys.argv[1]))
edges = d.get("symbol_graph", [])
communities = d.get("symbol_communities", [])

# build community color map
colors = ["#e6194b","#3cb44b","#4363d8","#f58231","#911eb4","#42d4f4",
          "#f032e6","#bfef45","#fabed4","#469990","#dcbeff","#9A6324",
          "#800000","#aaffc3","#808000","#000075"]
sym_color = {}
for c in communities:
    for s in c["symbols"]:
        sym_color[s["name"]] = colors[c["id"] % len(colors)]

# filter: only edges with weight >= 2 or type == "call"
edges = [e for e in edges if e["weight"] >= 2 or e["type"] == "call"]

# only include nodes that survived filtering
connected = set()
for e in edges:
    connected.add(e["source"])
    connected.add(e["target"])

shapes = {"function": "ellipse", "method": "ellipse", "class": "box",
          "struct": "box3d", "interface": "diamond", "constructor": "hexagon"}

print('digraph{overlap=prism;splines=true;rankdir=LR;')
print('node[style=filled,fontsize=8,fontname="Helvetica"];')
for name in connected:
    short = name.split(".")[-1]
    kind = None
    for e in edges:
        if e["source"] == name:
            kind = e["source_kind"]; break
        if e["target"] == name:
            kind = e["target_kind"]; break
    shape = shapes.get(kind or "", "ellipse")
    fill = sym_color.get(name, "#f0f0f0")
    print(f'"{name}"[label="{short}",shape={shape},fillcolor="{fill}"];')
for e in edges:
    pw = min(e["weight"], 6)
    style = "dashed" if e["type"] == "reference" else "solid"
    print(f'"{e["source"]}"->"{e["target"]}"[penwidth={pw},style={style}];')
print("}")