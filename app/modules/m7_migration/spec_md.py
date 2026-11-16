"""迁移单：把一次判定的结论整理成带条款号与脚注的 Markdown。"""

from __future__ import annotations

from app import config, reasons

FOOTNOTES = [
    "字段身份按 field_id 对齐，改名不算新字段。",
    "类型放宽指新类型能无损容纳旧类型的全部取值。",
    f"生效区间按 {config.WINDOW_SEMANTICS} 处理。",
    "账目差额应为 0；不为 0 说明判定漏项。",
]


def clauses(run: dict) -> list[dict]:
    """按优先级列出破坏项，每条一个条款号，并带判定范围标记。"""
    rows = [row for row in (run.get("diff") or []) if row["verdict"] == "BREAKING"]
    rows.sort(key=lambda row: (row["table"], row["old_name"] or row["new_name"] or ""))
    out: list[dict] = []
    for index, row in enumerate(rows, start=1):
        out.append({
            "no": f"3.{index}",
            "field_id": row["field_id"],
            "table": row["table"],
            "old_name": row["old_name"],
            "new_name": row["new_name"],
            "old_type": row["old_type"],
            "new_type": row["new_type"],
            "change": row["change"],
            "type_detail": row["type_detail"],
            "primary": row["primary"],
            "primary_label": reasons.label(row["primary"]),
            "reader_ok": row["reader_ok"],
            "writer_ok": row["writer_ok"],
            "ref_count": row["ref_count"],
            "reasons": row["reasons"],
            "in_scope": bool(row.get("in_scope", True)),
            "scope_status": row.get("scope_status") or "CONCLUSIVE",
            "scope_reason": row.get("scope_reason"),
        })
    return out


def formal_clauses(run: dict) -> list[dict]:
    return [item for item in clauses(run) if item["in_scope"]]


def withheld_clauses(run: dict) -> list[dict]:
    return [item for item in clauses(run) if not item["in_scope"]]


def _md_table(header: list[str], rows: list[list[str]]) -> str:
    lines = ["| " + " | ".join(header) + " |",
             "|" + "|".join(["---"] * len(header)) + "|"]
    for row in rows:
        lines.append("| " + " | ".join(str(cell) for cell in row) + " |")
    return "\n".join(lines)


def _scope(run: dict) -> dict:
    return run.get("scope") or {}


def _review_table_keys(run: dict) -> set[str]:
    scope = _scope(run)
    return set(scope.get("review_table_keys")
               or [name.lower() for name in scope.get("review_tables", [])])


def render(run: dict) -> str:
    ledger = run.get("ledger") or {}
    fields = ledger.get("fields") or {}
    refs = ledger.get("refs") or {}
    buckets = fields.get("buckets") or {}
    formal = ledger.get("formal") or {}
    scope = _scope(run)
    review_keys = _review_table_keys(run)
    parts = [
        f"# 迁移单 {run['run_id']}",
        "",
        f"- 版本：{run.get('version_label_old')} → {run.get('version_label_new')}",
        f"- 规则集：{run.get('ruleset')}",
        f"- 生成时间：{config.to_local_text(_created(run))}",
        f"- 生效区间：{run.get('effective_from')} 起，半开区间[^3]",
        "",
        "## 1 输入",
        "",
        _md_table(
            ["角色", "文件", "sha256"],
            [[role, name, digest] for role, (name, digest) in sorted(_inputs(run).items())],
        ),
        "",
        "## 2 字段账（全量，含待复核表）",
        "",
        _md_table(
            ["项", "数"],
            [["旧版本字段", fields.get("old_total", 0)],
             ["未变", buckets.get("unchanged", 0)],
             ["改名", buckets.get("renamed", 0)],
             ["换类型", buckets.get("retyped", 0)],
             ["改必填", buckets.get("required", 0)],
             ["冲突", buckets.get("conflict", 0)],
             ["删除", buckets.get("deleted", 0)],
             ["新增", buckets.get("added", 0)],
             ["新版本字段", fields.get("new_total", 0)],
             ["旧侧差额", fields.get("old_gap", 0)],
             ["新侧差额", fields.get("new_gap", 0)]],
        ),
        "",
        f"字段身份按 field_id 对齐[^1]，类型放宽按值域与刻度判定[^2]，账目差额应为 0[^4]。",
        "",
        "## 3 判定范围",
        "",
        _md_table(
            ["表", "范围", "原因", "阻断行", "受影响下游"],
            _scope_rows(scope),
        ),
        "",
        f"可出正式结论 {scope.get('conclusive_count', 0)} 张，停在待复核 "
        f"{scope.get('review_count', 0)} 张；待复核表的字段不进正式条款[^5]。",
        "",
        "## 4 正式破坏条款（仅判定范围内）",
        "",
    ]

    items = formal_clauses(run)
    withheld = withheld_clauses(run)
    if not items:
        parts.append("判定范围内无破坏项。")
        parts.append("")
    # 正式条款在范围内重新连续编号，避免被扣留项撑出断号
    for index, item in enumerate(items, start=1):
        parts.append(f"### 4.{index} {item['table']}.{item['old_name'] or '—'}"
                     f" → {item['new_name'] or '—'}")
        parts.append("")
        parts.append(f"- 原因码：{item['primary']}（{item['primary_label']}）")
        parts.append(f"- 类型：{item['old_type'] or '—'} → {item['new_type'] or '—'}"
                     f"，变化 {item['change']}")
        parts.append(f"- 读者：{'通过' if item['reader_ok'] else '不通过'}"
                     f"；写者：{'通过' if item['writer_ok'] else '不通过'}")
        parts.append(f"- 下游引用：{item['ref_count']} 处")
        parts.append("")

    parts.extend([
        "### 4.A 待复核扣留项（不出正式条款）",
        "",
    ])
    if not withheld:
        parts.append("无扣留项。")
        parts.append("")
    else:
        parts.append(_md_table(
            ["表", "字段", "原因码", "扣留原因"],
            [[item["table"],
              f"{item['old_name'] or '—'} → {item['new_name'] or '—'}",
              item["primary"], "SCOPE_REVIEW"] for item in withheld],
        ))
        parts.append("")

    parts.extend([
        "## 5 映射",
        "",
        "### 5.1 正式别名（判定范围内）",
        "",
        "```sql",
    ])
    aliases = (run.get("mapping") or {}).get("aliases") or []
    formal_aliases = [item for item in aliases
                      if item["table"].lower() not in review_keys]
    withheld_aliases = [item for item in aliases
                        if item["table"].lower() in review_keys]
    if formal_aliases:
        for item in formal_aliases:
            parts.append(f"{item['sql']}  -- {item['table']}")
    else:
        parts.append("-- 判定范围内无改名，无需别名")
    parts.extend([
        "```",
        "",
        "### 5.2 待复核别名（扣留，不随正式单下发）",
        "",
        "```sql",
    ])
    if withheld_aliases:
        for item in withheld_aliases:
            parts.append(f"-- [SCOPE_REVIEW] {item['table']}：{item['sql']}")
    else:
        parts.append("-- 无扣留别名")
    parts.extend([
        "```",
        "",
        "## 6 影响面",
        "",
        _md_table(
            ["项", "数"],
            [["下游节点", (run.get("impact") or {}).get("summary", {}).get("consumers", 0)],
             ["登记引用", refs.get("refs_total", 0)],
             ["可达", refs.get("reachable", 0)],
             ["断链", refs.get("broken", 0)],
             ["受影响节点（全量）", (run.get("impact") or {}).get("impact", {}).get("total", 0)],
             ["待复核表波及下游", formal.get("affected_count", 0)],
             ["环", (run.get("impact") or {}).get("cycles", {}).get("cycle_count", 0)],
             ["自引用", (run.get("impact") or {}).get("cycles", {}).get("self_loop_count", 0)],
             ["单边登记", len((run.get("impact") or {}).get("one_sided") or [])]],
        ),
        "",
        "## 7 回滚",
        "",
        "1. 按第 5.1 节建兼容视图，旧名字继续可用；待复核别名先不下发。",
        "2. 正式破坏项逐个补默认值或改类型；待复核表修好坏行、补齐证据后再重跑判定。",
        "3. 版本切回上一版，生效区间端点保持半开。",
        "",
    ])
    all_notes = FOOTNOTES + [
        "待复核表的字段记录全部保留，只是不进正式兼容结论与迁移条款。",
    ]
    for index, note in enumerate(all_notes, start=1):
        parts.append(f"[^{index}]: {note}")
    parts.append("")
    return "\n".join(parts)


def _scope_rows(scope: dict) -> list[list[str]]:
    rows: list[list[str]] = []
    for item in scope.get("tables", []):
        affected = " ".join(c["name"] for c in item.get("affected_consumers", [])) or "—"
        rows.append([
            item["table"],
            "待复核" if item["status"] == "REVIEW" else "正式",
            ";".join(item.get("reasons") or []) or "—",
            str(item.get("blocked_count", 0)),
            affected,
        ])
    return rows


def _created(run: dict):
    from datetime import datetime

    raw = run.get("created_at")
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        return None


def _inputs(run: dict) -> dict[str, tuple[str, str]]:
    names = run.get("input_names") or {}
    digests = run.get("digests") or {}
    return {role: (names.get(role, "—"), digests.get(role, "—")) for role in digests}
