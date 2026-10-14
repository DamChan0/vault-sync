#!/usr/bin/env python3
"""Build/query a local Graphify-style graph for an Obsidian vault."""
from __future__ import annotations
import argparse, json, re
from collections import defaultdict, deque
from pathlib import Path

import vault_config
LINK = re.compile(r"\[\[([^\]|#]+)(?:#[^\]|]+)?(?:\|[^\]]+)?\]\]")
FM = re.compile(r"\A---\n(.*?)\n---\n", re.S)
FIELD = re.compile(r"^(aliases|keywords|tags|related):\s*\[(.*?)\]\s*$", re.M)

def values(value: str) -> list[str]:
    return [x.strip().strip("\"'") for x in value.split(",") if x.strip().strip("\"'")]
def build(vault: Path, output: Path, exclude=()) -> dict:
    dirs = vault_config.default_indexed_dirs(vault, exclude=exclude)
    files = sorted(p for directory in dirs for p in (vault / directory).rglob("*.md") if p.is_file())
    stems = {p.stem: p for p in files}
    nodes, edges = [], {}
    for path in files:
        text = path.read_text(encoding="utf-8")
        match = FM.match(text)
        fields = {k: values(v) for k, v in FIELD.findall(match.group(1) if match else "")}
        rel = path.relative_to(vault).as_posix()
        title = fields.get("title", [path.stem])[0]
        nodes.append({"id": rel, "label": path.stem, "path": rel, "title": title, "aliases": fields.get("aliases", []), "keywords": fields.get("keywords", []), "tags": fields.get("tags", [])})
        targets = LINK.findall(text) + fields.get("related", [])
        for target in targets:
            target = target.strip().strip("\"'")
            other = stems.get(target)
            if not other or other == path:
                continue
            dest = other.relative_to(vault).as_posix()
            edges[(rel, dest)] = {"source": rel, "target": dest, "relation": "references", "confidence": "EXTRACTED"}
    graph = {"schema": 1, "source": "vault markdown", "nodes": nodes, "edges": list(edges.values()), "stats": {"nodes": len(nodes), "edges": len(edges)}}
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(graph, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return graph
def query(graph: dict, question: str, limit: int) -> list[dict]:
    terms = [x.lower() for x in re.findall(r"[\w가-힣-]+", question) if len(x) > 1]
    adjacency = defaultdict(set)
    for edge in graph["edges"]:
        adjacency[edge["source"]].add(edge["target"])
        adjacency[edge["target"]].add(edge["source"])
    lexical = {}
    for node in graph["nodes"]:
        haystack = " ".join([node["label"], node["title"], *node["aliases"], *node["keywords"], *node["tags"]]).lower()
        lexical[node["id"]] = sum(haystack.count(term) for term in terms)
    seeds = [node_id for node_id, score in sorted(lexical.items(), key=lambda item: (-item[1], item[0])) if score][:limit]
    distance = {node_id: 0 for node_id in seeds}
    queue = deque(seeds)
    while queue:
        current = queue.popleft()
        if distance[current] >= 1:
            continue
        for neighbor in adjacency[current]:
            if neighbor not in distance:
                distance[neighbor] = 1
                queue.append(neighbor)
    relevance = {node_id: lexical[node_id] for node_id in distance}
    for seed in seeds:
        for neighbor in adjacency[seed]:
            relevance[neighbor] = max(relevance.get(neighbor, 0), lexical[seed] / 2)
    by_id = {node["id"]: node for node in graph["nodes"]}
    ranked = sorted(distance, key=lambda node_id: (-relevance[node_id], distance[node_id], node_id))
    return [{"path": by_id[node_id]["path"], "title": by_id[node_id]["title"], "lexical_score": lexical[node_id], "graph_score": relevance[node_id], "distance": distance[node_id]} for node_id in ranked[:limit]]

def main() -> None:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    b = sub.add_parser("build")
    b.add_argument("vault", type=Path, nargs="?", help="Default: $VAULT_SYNC_DIR or ~/.config/vault-sync/profile.json.")
    b.add_argument("--output", type=Path, help="Default: <vault>/.vault-meta/graphify/graph.json")
    b.add_argument("--exclude", default="", help="Comma-separated top-level folders to keep out (see _ai/CLAUDE.md).")
    q = sub.add_parser("query"); q.add_argument("graph", type=Path, nargs="?", help="Default: <vault>/.vault-meta/graphify/graph.json"); q.add_argument("question"); q.add_argument("--top", type=int, default=5)
    args = parser.parse_args()
    if args.command == "build":
        vault = args.vault or vault_config.load_vault_dir()
        output = args.output or (vault / ".vault-meta" / "graphify" / "graph.json")
        print(json.dumps(build(vault, output, exclude=args.exclude.split(","))["stats"], ensure_ascii=False))
    else:
        graph_path = args.graph or (vault_config.load_vault_dir() / ".vault-meta" / "graphify" / "graph.json")
        print(json.dumps(query(json.loads(graph_path.read_text(encoding="utf-8")), args.question, args.top), ensure_ascii=False, indent=2))
if __name__ == "__main__": main()
