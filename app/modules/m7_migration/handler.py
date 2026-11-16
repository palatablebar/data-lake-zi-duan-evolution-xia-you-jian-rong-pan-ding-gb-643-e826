"""M7 入口：迁移单页与导出物。"""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, Response

from app import store, web
from app.modules.m7_migration import service

router = APIRouter()

CSV_HEADERS = {"Content-Disposition": 'attachment; filename="fieldmap.csv"'}


def _csv(body: str, filename: str) -> Response:
    return Response(
        content=body,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/spec", response_class=HTMLResponse)
def spec_page(request: Request):
    run = store.latest_run()
    all_clauses = service.clauses(run) if run else []
    formal = [item for item in all_clauses if item.get("in_scope", True)]
    withheld = [item for item in all_clauses if not item.get("in_scope", True)]
    review_keys = set(((run or {}).get("scope") or {}).get("review_table_keys") or [])
    aliases = ((run or {}).get("mapping") or {}).get("aliases") or []
    formal_aliases = [item for item in aliases if item["table"].lower() not in review_keys]
    withheld_aliases = [item for item in aliases if item["table"].lower() in review_keys]
    return web.page_or_404(
        request,
        "spec.html",
        "spec",
        run,
        clauses=formal,
        withheld_clauses=withheld,
        ledger=(run or {}).get("ledger") or {},
        aliases=formal_aliases,
        withheld_aliases=withheld_aliases,
        scope=((run or {}).get("scope") or {}),
        warnings=(run or {}).get("warnings") or [],
    )


@router.get("/api/export/spec.md")
def export_spec():
    run = service.latest_or_404()
    return Response(
        content=service.spec_markdown(run),
        media_type="text/markdown; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{run["run_id"]}.md"'},
    )


@router.get("/api/export/fields.csv")
def export_fields():
    run = service.latest_or_404()
    return _csv(service.fields_csv_text(run), "fieldmap-fields.csv")


@router.get("/api/export/refs.csv")
def export_refs():
    run = service.latest_or_404()
    return _csv(service.refs_csv_text(run), "fieldmap-refs.csv")


@router.get("/api/export/ledger.csv")
def export_ledger():
    run = service.latest_or_404()
    return _csv(service.ledger_csv_text(run), "fieldmap-ledger.csv")


@router.get("/api/export/scope.csv")
def export_scope():
    run = service.latest_or_404()
    return _csv(service.scope_csv_text(run), "fieldmap-scope.csv")


@router.get("/api/export/manifest.json")
def export_manifest():
    run = service.latest_or_404()
    return Response(
        content=service.manifest_json_text(run),
        media_type="application/json; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{run["run_id"]}.json"'},
    )
