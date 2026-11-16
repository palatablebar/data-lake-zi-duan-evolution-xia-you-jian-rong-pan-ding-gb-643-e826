"""M8 判定范围编排：表级范围、待复核原因、受影响下游、修订与导出。"""

from __future__ import annotations

from app.modules.m8_scope import propagate, revision, scope


def compute_run_scope(run: dict) -> dict:
    """从一次批次的快照与隔离区计算范围，并把闭包下游挂进同一份结构。

    页面与所有导出只读这份结构，保证范围、原因、受影响下游完全一致。
    破坏项的正式/扣留拆分依赖逐字段行的 in_scope 标记，若调用方已先打标
    （m6 流水线在 annotate_diff 之后回填）就直接采用，否则默认全部在范围内。
    """
    consumers = (run.get("refs") or {}).get("consumers") or []
    result = scope.build_scope(
        run.get("fields_old") or [],
        run.get("fields_new") or [],
        run.get("quarantine_old") or [],
        run.get("quarantine_new") or [],
        consumers,
    )
    review_tables = [item for item in result["tables"] if item["status"] == scope.REVIEW]
    closure_payload = propagate.propagate(review_tables, consumers)

    # 直接引用名单（表级）与反向闭包名单并列，口径分开但同源
    direct_names = sorted({
        consumer["name"]
        for item in review_tables
        for consumer in item["affected_consumers"]
    })
    result["downstream_closure"] = closure_payload
    result["affected_consumers"] = closure_payload["names"]
    result["affected_count"] = closure_payload["total"]
    result["affected_direct"] = direct_names
    result["formal_breaking"] = [
        row["field_id"] for row in (run.get("diff") or [])
        if row.get("verdict") == "BREAKING" and row.get("in_scope", True)
    ]
    result["withheld_breaking"] = [
        row["field_id"] for row in (run.get("diff") or [])
        if row.get("verdict") == "BREAKING" and not row.get("in_scope", True)
    ]
    return result


def refresh_breaking_split(scope_payload: dict, rows: list[dict]) -> dict:
    """逐字段行打上范围标记后，回填正式/扣留破坏项拆分。"""
    scope_payload["formal_breaking"] = [
        row["field_id"] for row in rows
        if row.get("verdict") == "BREAKING" and row.get("in_scope", True)
    ]
    scope_payload["withheld_breaking"] = [
        row["field_id"] for row in rows
        if row.get("verdict") == "BREAKING" and not row.get("in_scope", True)
    ]
    return scope_payload


def scope_csv_text(scope_payload: dict) -> str:
    """范围导出：每张表一行，范围、原因、受影响下游与页面同一份。

    统一走 m7 的 csv_out.scope_csv，保证页面、M8 接口与 M7 导出字节一致。
    """
    from app.modules.m7_migration import csv_out

    return csv_out.scope_csv(scope_payload)


def scope_summary(scope_payload: dict) -> dict:
    return {
        "table_count": scope_payload.get("table_count", 0),
        "conclusive_count": scope_payload.get("conclusive_count", 0),
        "review_count": scope_payload.get("review_count", 0),
        "review_tables": scope_payload.get("review_tables", []),
        "conclusive_tables": scope_payload.get("conclusive_tables", []),
        "affected_count": scope_payload.get("affected_count", 0),
        "blocked_total": scope_payload.get("blocked_total", 0),
    }
