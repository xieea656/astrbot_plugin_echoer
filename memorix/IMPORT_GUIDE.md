# Echoer 导入中心指南

本文档仅描述当前 `/import` 页面与 `/v1/import/*` 增强接口的实际能力。

## 功能范围

当前仅支持 3 类导入：

1. 上传文件导入
2. 粘贴文本导入
3. 原始目录扫描导入

不包含：`lpmm_openie`、`lpmm_convert`、`maibot_migration`、`temporal_backfill`。

## 支持格式与类型

- 允许文件后缀：`.txt`、`.md`、`.json`
- 可手动指定 `knowledge_type`：
  - `auto`（自动识别）
  - `factual`
  - `narrative`
  - `structured`
  - `mixed`

说明：

- 文本导入（`.txt/.md`、粘贴文本）会按段落分块写入。
- JSON 导入按单元写入（`paragraph` / `relation`）。
- `knowledge_type=auto` 会在写入段落时使用自动识别。

## 页面入口

- 页面路由：`GET /import`
- 默认关闭，需要配置：`web.import.enabled = true`

## 任务能力

导入任务具备三层状态观测：

1. 任务级（task）
2. 文件级（file）
3. 分块级（chunk）

并支持：

- 取消任务：`POST /v1/import/tasks/{task_id}/cancel`
- 重试失败：`POST /v1/import/tasks/{task_id}/retry_failed`
  - 优先重试失败分块
  - 无可重试分块时回退到失败文件级重跑

## 增强接口清单

- `POST /v1/import/tasks/upload`
- `POST /v1/import/tasks/paste`
- `POST /v1/import/tasks/raw_scan`
- `GET /v1/import/tasks`
- `GET /v1/import/tasks/{task_id}`
- `GET /v1/import/tasks/{task_id}/files/{file_id}/chunks`
- `POST /v1/import/tasks/{task_id}/cancel`
- `POST /v1/import/tasks/{task_id}/retry_failed`
- `GET /v1/import/path_aliases`
- `POST /v1/import/path_resolve`

兼容入口保留：

- `POST /v1/import/tasks`
- `GET /v1/import/tasks/{task_id}`（增强任务不存在时回退 legacy）

## 目录扫描安全约束

- 仅允许 alias：`raw`、`plugin_data`
- 必须使用 `alias + relative_path` 解析
- 禁止绝对路径、盘符路径与越界路径（`..`）

## 写保护

导入任务运行中，除 `/v1/import/*` 外的写请求会返回 `409`，用于避免并发写入冲突。

## 配置项

- `web.import.enabled`（默认 `false`）
- `web.import.max_queue_size`
- `web.import.max_files_per_task`
- `web.import.max_file_size_mb`
- `web.import.max_paste_chars`
- `web.import.default_file_concurrency`（预留）
- `web.import.default_chunk_concurrency`（预留）
- `web.import.path_aliases.raw`
- `web.import.path_aliases.plugin_data`
