---
name: chip-230-data-query
description: Query chip-230 Tablestore tables and OSS documents with a one-shot CLI, without starting a web server. Use when an agent needs to list chip-230 tables, query oss_documents/davine_oss_documents/plans, find documents by tag or category, get document/plan metadata, or download a document/plan. Triggers include "query chip-230 documents", "find documents tagged env", "oss_documents tag query", "list chip-230 tables", "download document from chip-230".
license: Apache-2.0
metadata:
  version: 1.0.0
---

# chip-230 Data Query

一次性查询 chip-230 的 Tablestore 表和 OSS 文档。**不要启动 `web/app.py`**；本工具直接读取仓库 `.env`，执行一次、输出 JSON、退出。

## 适用场景

- 列出可查询的表和字段；
- 查询 `oss_documents` / `davine_oss_documents` / `plans`；
- 按标签/分类查找文档，例如 `tag=env`；
- 获取单份文档或计划的元数据和 OSS 下载链接；
- 把文档下载到本地文件。

## 前置条件

- 在 chip-230 仓库根目录执行（或设置 `CHIP230_ROOT` 指向仓库根目录）；
- 已准备 `./.venv` 并安装 `requirements.txt`；
- 根目录 `.env` 已配置 `ALIBABA_CLOUD_ACCESS_KEY_ID/SECRET`、`OSS_BUCKET`、`OTS_INSTANCE` 等存储配置；
- **不需要** token，不需要监听端口。

先做一次配置检查：

```bash
./.venv/bin/python infra/agent_query.py health
```

## 命令

| 目的 | 命令 |
| --- | --- |
| 配置与表清单 | `./.venv/bin/python infra/agent_query.py health` |
| 表结构 | `./.venv/bin/python infra/agent_query.py tables` |
| 按标签查文档 | `./.venv/bin/python infra/agent_query.py documents --tag env` |
| 通用表查询 | `./.venv/bin/python infra/agent_query.py rows oss_documents --tag env --limit 20` |
| 单份文档 + 下载链接 | `./.venv/bin/python infra/agent_query.py document <doc_id> --expires 24` |
| 下载文档 | `./.venv/bin/python infra/agent_query.py download-document <doc_id> --out /tmp/doc.md` |
| 查询计划 | `./.venv/bin/python infra/agent_query.py plans --limit 10` |
| 单条计划 + 下载链接 | `./.venv/bin/python infra/agent_query.py plan <uuid> <id> --expires 24` |
| 下载计划 | `./.venv/bin/python infra/agent_query.py download-plan <uuid> <id> --out /tmp/plan.md` |

通用参数：

- `--tag TAG`：可重复，也可逗号分隔；大小写不敏感；
- `--tag-mode any|all`：默认 `any`；
- `--category`、`--q`、`--since`、`--until`；
- `--github-project-name`、`--github-project-branch`：仅 `plans`；
- `--limit`（默认 50，最大 200）、`--offset`（最大 10000）；
- `--compact`：单行 JSON，便于 Agent 解析；
- `document` / `download-document` 的 `--source documents|davine`；
- `--expires`：下载链接有效期，1–168 小时。

## 标签语义

`oss_documents` 目前只有 `category` 字段。`tag` 会匹配：

1. `category`（现有上传流程写入的分类）；
2. 行里存在 `tags` 列时，再匹配 `tags`（逗号分隔或 JSON 数组）。

示例：

```bash
# category=env 或 tags 包含 env
./.venv/bin/python infra/agent_query.py documents --tag env --compact

# 任一标签
./.venv/bin/python infra/agent_query.py documents --tag paper,agents

# 同时包含
./.venv/bin/python infra/agent_query.py documents --tag paper,agents --tag-mode all
```

当前上传流程只写 `category`，所以 `tag=env` 通常等价于 `category=env`。要多标签，需要在行里补 `tags` 列。

## 输出契约

- 成功：JSON 输出到 stdout，退出码 `0`；
- 失败：`{"ok": false, "error": "..."}` 输出到 stderr，退出码：
  - `2`：参数错误；
  - `3`：缺少 OSS / Tablestore 配置；
  - `4`：表或文档不存在；
  - `1`：其它错误。
- 列表响应含 `count` / `total` / `truncated` / `has_more` / `next_offset`；
- `truncated=true` 表示 Tablestore 扫描达到 10,000 行上限，需缩小过滤条件；
- `document` / `plan` 返回 `download_url` 和 `expires_at`；`download-*` 会把内容写到 `--out` 指定的文件。

## 安全边界

- 工具只读，不提供写入、删除；
- 工具以本机 `.env` 的云凭据运行；只把 skill 交给可信 Agent，并限制 `.env` 的读权限；
- `.env` 中的 AK、OSS/Tablestore 凭据不会被打印；
- 下载链接是临时签名 URL，不要写进公开日志或提交到 Git；
- 单次查询最多扫描 10,000 行；数据量大时改用多元索引/ES。

## 作为 Skill 使用

- 本目录的 `SKILL.md` 是 skill 入口，`agent_query.py` 是执行入口；
- 把本目录作为 skill 加载，或复制 `SKILL.md`、`agent_query.py`、`agent_core.py`、`__init__.py` 到 Agent 的 skills 目录；
- 如果加载器要求目录名与 `name` 一致，把整个 `infra/` 复制/软链为 `chip-230-data-query/` 再加载；
- 若复制到仓库外，设置 `CHIP230_ROOT=/path/to/chip-230`，并确保该路径下有 `.venv` 和 `.env`；

