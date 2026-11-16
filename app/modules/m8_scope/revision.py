"""判定范围的修订与落盘：原子写、旧版备份、失败保留与可恢复。

落盘顺序保证「新范围落盘失败时旧版仍在、且能恢复」：
1. 目标已存在就先复制成备份（同目录 .bak），备份不动旧文件本身；
2. 新内容写到同目录临时文件，再 os.replace 原子替换；
3. 任一步抛错都不碰旧文件，调用方拿到 ScopePersistError，旧版继续可用；
4. 回滚 = 用备份覆盖回目标，同样走临时文件 + 原子替换。

SQLite 的 scope_revisions 是权威历史（每版都留），文件快照只是外加的可恢复副本。
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
from pathlib import Path

from app import config, errors, store


class ScopePersistError(errors.FieldmapError):
    status = 500
    code = "SCOPE_PERSIST_FAILED"
    label = "范围落盘失败"


def scope_dir() -> Path:
    raw = os.environ.get("FIELDMAP_SCOPE_DIR", "").strip()
    base = Path(raw) if raw else config.PROJECT_ROOT / "data" / "scope"
    base.mkdir(parents=True, exist_ok=True)
    return base


def scope_path(run_id: str) -> Path:
    safe = "".join(ch for ch in str(run_id) if ch.isalnum() or ch in ("-", "_"))
    if not safe:
        raise errors.BadInput("run_id 非法", detail={"run_id": run_id})
    return scope_dir() / f"{safe}.scope.json"


def backup_path(run_id: str) -> Path:
    target = scope_path(run_id)
    return target.with_suffix(".bak.json")


def revision_id_for(run_id: str, scope: dict, note: str | None) -> str:
    """修订号由范围内容与备注推导：同一份修订重跑得到同一个 id，不重复建行。"""
    canonical = json.dumps(
        {"run_id": run_id, "scope": scope, "note": note or ""},
        ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    )
    return "scp-" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:12]


def _atomic_write(path: Path, text: str) -> None:
    tmp = path.with_name(path.name + ".tmp")
    try:
        with open(tmp, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except OSError as exc:
        # 临时文件清不掉也不影响旧版
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise ScopePersistError(
            "判定范围落盘失败，已保留旧版",
            detail={"path": str(path), "reason": str(exc), "kept": "previous"},
        ) from exc


def write_snapshot(run_id: str, revision: dict) -> Path:
    """备份旧版 → 原子写新版。失败时旧文件原封不动。"""
    target = scope_path(run_id)
    if target.exists():
        try:
            shutil.copy2(target, backup_path(run_id))
        except OSError as exc:
            raise ScopePersistError(
                "判定范围备份失败，已保留旧版",
                detail={"path": str(target), "reason": str(exc), "kept": "previous"},
            ) from exc
    text = json.dumps(revision, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    _atomic_write(target, text)
    return target


def persist_revision(run_id: str, scope: dict, note: str | None = None,
                     status: str = "ACTIVE") -> dict:
    """落一条范围修订：SQLite 权威历史 + 文件快照，两者都保留旧版可恢复。

    幂等：相同 (run_id, 范围内容, 备注) 命中已有 revision_id，直接复用、不新增。
    """
    revision_id = revision_id_for(run_id, scope, note)
    existing = store.scope_revision_by_id(revision_id)
    if existing is not None:
        existing["reused"] = True
        return existing

    revision = {
        "revision_id": revision_id,
        "run_id": run_id,
        "revision_seq": store.next_scope_seq(run_id),
        "status": status,
        "note": note or "",
        "created_at": config.now_utc().isoformat(),
        "scope": scope,
        "reused": False,
    }
    # 先写文件快照（失败直接抛，库内不留下没有快照的修订），再登记历史
    path = write_snapshot(run_id, revision)
    revision["snapshot_path"] = str(path)
    store.save_scope_revision(revision)
    return revision


def rollback(run_id: str) -> dict:
    """回滚到上一版范围；没有备份时明确报错，绝不返回半成品。"""
    target = scope_path(run_id)
    backup = backup_path(run_id)
    if not backup.exists():
        raise errors.NotFound("没有可回滚的范围备份", detail={"run_id": run_id})
    try:
        text = backup.read_text(encoding="utf-8")
        previous = json.loads(text)
    except (OSError, json.JSONDecodeError) as exc:
        raise ScopePersistError(
            "范围备份无法读取，已保留现状",
            detail={"backup": str(backup), "reason": str(exc)},
        ) from exc
    _atomic_write(target, text)
    previous = dict(previous)
    previous["status"] = "ROLLED_BACK"
    previous["rolled_back_at"] = config.now_utc().isoformat()
    previous["revision_id"] = revision_id_for(run_id, previous.get("scope") or {},
                                              "rollback")
    previous["revision_seq"] = store.next_scope_seq(run_id)
    store.save_scope_revision(previous)
    return previous


def current_revision(run_id: str) -> dict | None:
    return store.latest_scope_revision(run_id)


def list_scope_revisions(run_id: str) -> list[dict]:
    return store.list_scope_revisions(run_id)
