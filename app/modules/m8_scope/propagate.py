"""待复核表的受影响下游：直接引用 + 反向闭包，深度取最短跳数。

与 M5 影响面共用同一套依赖图语义（上下游两边登记合并、按层推进），
保证页面上「受影响下游」和影响面页的闭包口径一致。
"""

from __future__ import annotations

from app import config
from app.modules.m5_impact import closure


def _direct_consumers(table_key: str, consumers: list[dict]) -> set[str]:
    names: set[str] = set()
    for consumer in consumers:
        tables = {ref.rpartition(".")[0].strip().lower() for ref in consumer.get("refs", [])}
        if table_key in tables:
            names.add(consumer["name"])
    return names


def propagate(scope_tables: list[dict], consumers: list[dict],
              max_depth: int | None = None) -> dict:
    """从每张待复核表的直接下游出发，沿反向依赖闭包展开。

    返回 {下游节点名: {depth, via, sources: [表名, ...]}}；
    depth=1 表示直接引用待复核表，之后每跳一层 +1，取最短跳数。
    """
    limit = max_depth or config.MAX_IMPACT_DEPTH
    downstream = closure.downstream_graph(closure.dependency_graph(consumers))

    seeds: dict[str, set[str]] = {}
    depth: dict[str, int] = {}
    via: dict[str, str] = {}
    for table in scope_tables:
        key = table["table_key"]
        label = table["table"]
        for name in sorted(_direct_consumers(key, consumers)):
            seeds.setdefault(name, set()).add(label)
            depth.setdefault(name, 1)
            via.setdefault(name, "scope")

    queue = sorted(depth)
    # 记录每个节点的直接上游，供把来源待复核表沿闭包一路带到末梢
    parent: dict[str, str] = {}
    while queue:
        node = queue.pop(0)
        if depth[node] >= limit:
            continue
        for nxt in sorted(downstream.get(node, ())):
            if nxt == node or nxt in depth:
                continue
            depth[nxt] = depth[node] + 1
            via[nxt] = node
            parent[nxt] = node
            queue.append(nxt)

    def _sources_of(name: str) -> set[str]:
        current = name
        while current in parent:
            current = parent[current]
        return set(seeds.get(current, set()))

    kinds = {item["name"]: item.get("kind") or "task" for item in consumers}
    rows = [
        {
            "name": name,
            "kind": kinds.get(name, "task"),
            "depth": depth[name],
            "via": via[name],
            "sources": sorted(seeds.get(name, set()) or _sources_of(name)),
        }
        for name in sorted(depth, key=lambda item: (depth[item], item))
    ]
    return {
        "rows": rows,
        "total": len(rows),
        "names": sorted(row["name"] for row in rows),
        "direct": sorted({name for name, value in depth.items() if value == 1}),
        "max_depth": max((row["depth"] for row in rows), default=0),
    }
