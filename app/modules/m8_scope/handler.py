"""M8 入口：判定范围页、范围接口、修订/回滚、范围导出。"""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, Response

from app import errors, store, web
from app.modules.m8_scope import revision, service

router = APIRouter()


def _load_run_or_404() -> dict:
    run = store.latest_run()
    if not run:
        raise errors.NotFound("还没有批次，先在版本历史页跑一次")
    return run


def _csv(body: str, filename: str) -> Response:
    return Response(
        content=body,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/scope", response_class=HTMLResponse)
def scope_page(request: Request):
    run = store.latest_run()
    scope_payload = (run or {}).get("scope") or {}
    revisions = revision.list_scope_revisions(run["run_id"]) if run else []
    return web.page_or_404(
        request,
        "scope.html",
        "scope",
        run,
        scope=scope_payload,
        summary=service.scope_summary(scope_payload) if scope_payload else {},
        revisions=revisions,
        closure=(scope_payload.get("downstream_closure") or {}) if scope_payload else {},
    )


@router.get("/api/scope")
def scope_api():
    run = _load_run_or_404()
    return {
        "run_id": run["run_id"],
        "scope": run.get("scope") or {},
        "summary": service.scope_summary(run.get("scope") or {}),
    }


@router.get("/api/scope/export.csv")
def scope_export_csv():
    run = _load_run_or_404()
    scope_payload = run.get("scope") or {}
    return _csv(service.scope_csv_text(scope_payload), "fieldmap-scope.csv")


@router.get("/api/scope/revisions")
def scope_revisions_api():
    run = _load_run_or_404()
    rows = revision.list_scope_revisions(run["run_id"])
    return {
        "run_id": run["run_id"],
        "revisions": [
            {
                "revision_id": item["revision_id"],
                "revision_seq": item["revision_seq"],
                "status": item["status"],
                "note": item.get("note") or "",
                "created_at": item["created_at"],
                "summary": service.scope_summary(item.get("scope") or {}),
            }
            for item in rows
        ],
    }


@router.post("/api/scope/revisions")
def scope_create_revision(payload: dict | None = None):
    """按当前批次数据重算并落一版范围。相同内容命中幂等键，不重复建行。"""
    body = payload or {}
    note = str(body.get("note") or "").strip() or "手动重算"
    run = _load_run_or_404()
    computed = service.compute_run_scope(run)
    saved = revision.persist_revision(run["run_id"], computed, note=note)
    return {
        "revision_id": saved["revision_id"],
        "revision_seq": saved["revision_seq"],
        "reused": bool(saved.get("reused")),
        "summary": service.scope_summary(computed),
    }


@router.post("/api/scope/rollback")
def scope_rollback():
    run = _load_run_or_404()
    previous = revision.rollback(run["run_id"])
    return {
        "revision_id": previous["revision_id"],
        "revision_seq": previous["revision_seq"],
        "status": previous["status"],
        "summary": service.scope_summary(previous.get("scope") or {}),
    }
