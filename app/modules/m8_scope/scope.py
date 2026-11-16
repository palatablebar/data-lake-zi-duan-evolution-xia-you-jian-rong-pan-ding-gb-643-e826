"""表级判定范围：哪些表能出正式兼容结论，哪些必须停在待复核。

朴素实现有两个死法：
1. 坏行所在表跟着整批停住 —— 证据完整的其他表一起失去判定；
2. 直接把坏行跳过 —— 缺字段被误成新增、旧字段被误成删除，迁移单据此放错消费方。

这里按「表」隔离：阻断行只让所在表进入待复核，其他表照常出正式结论；
待复核表的字段行仍然逐条算出来（不丢记录），只是全部标记为范围外、不进正式迁移单。
判定只认「阻断级」诊断；提示级（派生身份、未知类型、编码回退）照常参算、不挡结论。
"""

from __future__ import annotations

CONCLUSIVE = "CONCLUSIVE"
REVIEW = "REVIEW"

# 待复核原因 → 是否阻断（阻断级坏行挡结论；单侧出现只提示）
BLOCKING_REASONS = {"LINE_MALFORMED", "DUPLICATE_COLUMN", "FIELD_ID_DUP", "TABLE_UNPARSED"}


def blocking_tables(quarantine: list[dict]) -> dict[str, list[dict]]:
    """从隔离区抽出「阻断级坏行落在哪些表」，保留每条坏行的完整记录。

    双重条件：采信层已标 blocking，且原因码属于已知阻断码（防御纵深，
    避免未来新增的非阻断码误被当成挡结论的坏行）。表名为空的文件级坏行
    （TABLE_UNPARSED）无法定位到表，由调用方决定如何兜底。
    """
    out: dict[str, list[dict]] = {}
    for row in quarantine:
        if not row.get("blocking") or row.get("reason") not in BLOCKING_REASONS:
            continue
        table = (row.get("table") or "").strip()
        if not table:
            continue
        out.setdefault(table.lower(), []).append(dict(row))
    return out


def table_presence(fields_old: list[dict], fields_new: list[dict]) -> dict[str, dict]:
    """每个表在旧/新版本的出现情况与字段数。键统一用小写表名。"""
    presence: dict[str, dict] = {}

    def _eat(fields: list[dict], side: str) -> None:
        for field in fields:
            key = (field.get("table") or "").strip().lower()
            if not key:
                continue
            entry = presence.setdefault(key, {
                "table": field["table"], "old": False, "new": False,
                "old_fields": 0, "new_fields": 0,
            })
            entry[side] = True
            entry[f"{side}_fields"] += 1

    _eat(fields_old, "old")
    _eat(fields_new, "new")
    return presence


def _downstream_of(table_key: str, consumers: list[dict]) -> list[dict]:
    """直接引用该表的下游节点：只数真实写在 SQL 里的表名引用。"""
    hit: list[dict] = []
    for consumer in consumers:
        tables = {ref.rpartition(".")[0].strip().lower() for ref in consumer.get("refs", [])}
        if table_key in tables:
            hit.append({
                "name": consumer["name"],
                "kind": consumer.get("kind") or "task",
                "ref_count": sum(
                    1 for ref in consumer.get("refs", [])
                    if ref.rpartition(".")[0].strip().lower() == table_key
                ),
            })
    hit.sort(key=lambda item: item["name"])
    return hit


def build_scope(fields_old: list[dict], fields_new: list[dict],
                quarantine_old: list[dict], quarantine_new: list[dict],
                consumers: list[dict]) -> dict:
    """产出表级判定范围。这是范围、原因、受影响下游的唯一计算出处。"""
    blocked_old = blocking_tables(quarantine_old)
    blocked_new = blocking_tables(quarantine_new)
    presence = table_presence(fields_old, fields_new)

    # 阻断行可能指向一个解析不出任何字段的表，也要给它一行待复核
    for key in set(blocked_old) | set(blocked_new):
        presence.setdefault(key, {
            "table": key, "old": False, "new": False,
            "old_fields": 0, "new_fields": 0,
        })

    tables: list[dict] = []
    for key in sorted(presence):
        entry = presence[key]
        bad_rows = [dict(row, side="old") for row in blocked_old.get(key, [])]
        bad_rows += [dict(row, side="new") for row in blocked_new.get(key, [])]
        bad_rows.sort(key=lambda row: (row.get("side") or "", int(row.get("line") or 0),
                                       row.get("reason") or ""))
        one_sided = bool(entry["old"]) != bool(entry["new"])
        reasons = sorted({row["reason"] for row in bad_rows})
        if one_sided:
            reasons.append("SCOPE_ONE_SIDED")

        if bad_rows:
            status = REVIEW
            primary = "SCOPE_BLOCKED_ROW"
        elif one_sided:
            # 单侧出现且无坏行：证据自洽，仍可出结论（删除/新增），只留提示
            status = CONCLUSIVE
            primary = "SCOPE_ONE_SIDED"
        else:
            status = CONCLUSIVE
            primary = "COMPATIBLE"

        affected = _downstream_of(key, consumers) if status == REVIEW else []
        tables.append({
            "table": entry["table"],
            "table_key": key,
            "status": status,
            "primary": primary,
            "reasons": reasons,
            "one_sided": one_sided,
            "present_old": entry["old"],
            "present_new": entry["new"],
            "old_fields": entry["old_fields"],
            "new_fields": entry["new_fields"],
            "blocked_count": len(bad_rows),
            "blocked_rows": bad_rows,
            "affected_consumers": affected,
            "affected_count": len(affected),
        })

    review = [item["table"] for item in tables if item["status"] == REVIEW]
    conclusive = [item["table"] for item in tables if item["status"] == CONCLUSIVE]
    review_keys = {item["table_key"] for item in tables if item["status"] == REVIEW}
    affected_names = sorted({
        consumer["name"]
        for item in tables if item["status"] == REVIEW
        for consumer in item["affected_consumers"]
    })
    return {
        "tables": tables,
        "review_tables": review,
        "conclusive_tables": conclusive,
        "review_table_keys": sorted(review_keys),
        "table_count": len(tables),
        "review_count": len(review),
        "conclusive_count": len(conclusive),
        "affected_consumers": affected_names,
        "affected_count": len(affected_names),
        "blocked_total": sum(item["blocked_count"] for item in tables),
    }


def annotate_diff(rows: list[dict], scope: dict) -> list[dict]:
    """给每条字段判定打上范围标记；不改判定本身，记录一条不丢。"""
    review_keys = set(scope.get("review_table_keys") or ())
    review_names = {name.lower() for name in (scope.get("review_tables") or ())}
    for row in rows:
        key = (row.get("table") or "").strip().lower()
        in_scope = key not in review_keys and key not in review_names
        row["in_scope"] = in_scope
        row["scope_status"] = CONCLUSIVE if in_scope else REVIEW
        if not in_scope:
            row["scope_reason"] = "SCOPE_REVIEW"
        else:
            row["scope_reason"] = None
    return rows
