# 运行手册 fieldmap

## 1 启动

```bash
docker compose up -d --build
curl -s localhost:18202/api/health
```

宿主端口 `18202`，容器内 `8000`。启动时如果库是空的，服务会自动把 `sample_data/` 里的样例跑一遍，页面打开即有数据。

## 2 截图与验收命令

```bash
docker compose config --quiet
docker compose exec -T app python -m pytest -q
```

## 3 主流程

1. 打开 `http://localhost:18202/`，看规格单的字段清单与采信诊断。
2. `/types` 比较两个类型写法，看读者 / 写者两向结论。
3. `/mapping` 看旧名到新名、别名 SQL、冲突项。
4. `/impact` 看受影响节点、环、断链、单边登记。
5. `/batches` 点「跑一次」，再点「陈旧提交」验证 409。
6. `/spec` 导出迁移单与三份账目 CSV。

## 4 重建数据库

```bash
docker compose down -v
docker compose up -d --build
```

命名卷 `fieldmap-data` 挂着 `/data/fieldmap.sqlite`。删卷即清空批次、快照与隔离区。

## 5 接口

| 方法 | 路径 | 作用 |
|---|---|---|
| GET | `/api/health` | 服务与库状态、当前批次号 |
| POST | `/api/schemas/import` | 采信样例文件，返回快照摘要与诊断 |
| GET | `/api/schemas/quarantine` | 隔离区 JSON |
| GET | `/api/schemas/quarantine.csv` | 隔离区 CSV |
| GET | `/api/types` | 类型格与别名表 |
| POST | `/api/types/compare` | 两个类型写法比较 |
| POST | `/api/compat` | 一对版本的两向判定 |
| GET | `/api/mapping` | 映射表、别名、冲突 |
| GET | `/api/impact` | 影响面、环、断链 |
| POST | `/api/runs` | 跑批次，可带基线版本号与生效区间 |
| GET | `/api/batches` | 批次历史 |
| GET | `/api/reconcile` | 四账对账 |
| GET | `/api/export/spec.md` | 迁移单 Markdown |
| GET | `/api/export/fields.csv` | 字段账 CSV（含 scope_status/scope_reason 两列） |
| GET | `/api/export/refs.csv` | 引用账 CSV |
| GET | `/api/export/ledger.csv` | 账目 CSV（含正式/扣留破坏、待复核表与波及下游） |
| GET | `/api/export/scope.csv` | 判定范围 CSV（每表一行：范围、原因、阻断行、受影响下游） |
| GET | `/api/export/manifest.json` | 运行清单（含 scope 段） |
| GET | `/api/scope` | 当前批次判定范围 JSON |
| GET | `/api/scope/revisions` | 范围修订历史 |
| POST | `/api/scope/revisions` | 按当前数据重算并落一版范围（相同内容幂等） |
| POST | `/api/scope/rollback` | 回滚到上一版范围（用 `.bak` 原子恢复） |

## 6 失败处置

| 现象 | 原因 | 处置 |
|---|---|---|
| 409 `VERSION_STALE` | 提交带的基线序号落后 | 去掉 `base_version_seq` 或填当前序号 |
| 400 `WINDOW_INVERTED` | 生效区间起点不早于终点 | 检查 `effective_from` / `effective_to` |
| 400 `BAD_INPUT` | 文件名、参数或 JSON 结构不对 | 看返回的 `detail` |
| 404 `NOT_FOUND` | 还没跑过批次 / 没有可回滚备份 | 先在 `/batches` 点「跑一次」；回滚前先成功落过第二版 |
| 500 `SCOPE_PERSIST_FAILED` | 范围文件落盘失败（权限/磁盘） | 旧版与备份仍在，修好后重算或调 `/api/scope/rollback` |

### 判定范围文件

- SQLite 表 `scope_revisions` 是权威历史；文件快照在 `data/scope/<run_id>.scope.json`，上一版备份为同目录 `<run_id>.bak.json`。
- 可用环境变量 `FIELDMAP_SCOPE_DIR` 改快照目录；落盘走「备份 → 临时文件 → 原子替换」，失败不碰旧文件。
- 待复核表修好坏行、补齐证据后重新采信再跑批次；相同输入命中批次键复用，不重复建行。

## 7 端口与并发

同一批其他项目占用 18200-18209；本服务固定 18202。同时起多个栈时用批目录里的 `scripts/slot.sh` 限流。
