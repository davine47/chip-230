# chip-230 一次性查询工具

给 AI Agent 用的只读查询工具：**不启动 Web 服务、不监听端口**，执行一次、输出 JSON、退出。

```bash
./.venv/bin/python infra/agent_query.py health
./.venv/bin/python infra/agent_query.py documents --tag env
```

## 给其他 Agent 的接入约定

1. 只通过 shell 执行 `infra/agent_query.py`，不要启动 `web/app.py`，也不需要监听端口或申请 token。
2. 从 stdout 读 JSON；从 stderr 读 `{"ok": false, "error": ...}`，并按退出码判断类型。
3. 标签查询用 `documents --tag <tag>`；需要原始 OSS 文件时用 `download-document --out <path>`。
4. 工具必须在 chip-230 仓库根目录执行，或设置 `CHIP230_ROOT` 指向仓库根目录。
5. 单次最多扫描 10,000 行；看到 `truncated=true` 时缩小标签/时间/关键字条件。

## 文件

| 文件 | 用途 |
| --- | --- |
| `SKILL.md` | Agent Skill 入口（skill 加载时读这个） |
| `agent_query.py` | CLI：参数解析、查询、下载 |
| `agent_core.py` | 查询核心：表白名单、标签/分类/文本过滤、文档/计划查询 |
| `__init__.py` | 包标记，便于测试或其它 Agent 以模块方式导入 |

## 前置条件

在 chip-230 仓库根目录执行：

```bash
./.venv/bin/python infra/agent_query.py health
```

需要：

- `.venv` 已安装 `requirements.txt` 依赖；
- 根目录 `.env` 已配置 AccessKey、`OSS_BUCKET`、`OTS_INSTANCE` 等；
- 不需要 token，不需要启动 Web 服务。

## 常用命令

```bash
# 表结构 / 配置检查
./.venv/bin/python infra/agent_query.py tables
./.venv/bin/python infra/agent_query.py health

# 按标签查 oss_documents（env 是 category 或 tags）
./.venv/bin/python infra/agent_query.py documents --tag env

# 多标签、分页、单行 JSON
./.venv/bin/python infra/agent_query.py documents \
  --tag paper,agents --tag-mode any --limit 20 --compact

# 通用表查询：oss_documents / davine_oss_documents / plans
./.venv/bin/python infra/agent_query.py rows davine_oss_documents --q "索引"

# 取单份文档 + 下载链接
./.venv/bin/python infra/agent_query.py document <doc_id> --expires 24

# 下载到本地
./.venv/bin/python infra/agent_query.py download-document <doc_id> --out /tmp/doc.md

# 计划
./.venv/bin/python infra/agent_query.py plans --limit 10
./.venv/bin/python infra/agent_query.py plan <uuid> <id> --expires 24
./.venv/bin/python infra/agent_query.py download-plan <uuid> <id> --out /tmp/plan.md
```

`--compact` 放在子命令之后，例如 `documents --tag env --compact`。

## 输出与退出码

- 成功：JSON 到 stdout，退出码 `0`；
- 失败：`{"ok": false, "error": "..."}` 到 stderr；
  - `2` 参数错误；
  - `3` 缺少存储配置；
  - `4` 表/文档不存在；
  - `1` 其它错误。

列表结果包含 `count` / `total` / `truncated` / `has_more` / `next_offset`。`truncated=true` 表示扫描达到 10,000 行上限。

## 标签语义

`tag` 同时匹配：

1. `category`（现有上传流程写入的单分类）；
2. 可选的 `tags` 列（逗号分隔或 JSON 数组）。

大小写不敏感，`--tag-mode any|all` 控制任一/全部匹配。当前上传接口只写 `category`，所以 `tag=env` 通常就是按分类查询；要多标签需给行补 `tags` 列。

## 作为 Skill 分发

`SKILL.md` 是 skill 入口。复制或软链接本目录到 Agent 的 skills 目录时，至少带上：

```text
SKILL.md
agent_query.py
agent_core.py
__init__.py
```

若工具不在 chip-230 仓库内，设置：

```bash
export CHIP230_ROOT=/path/to/chip-230
```

并确保该路径下有 `.venv` 和 `.env`。


## 限制

- 只读，无写入/删除；
- Tablestore 查询是扫描 + 内存过滤，上限 10,000 行；
- `truncated=true` 时请缩小 `--tag` / `--q` / `--since` 等条件；
- 下载链接是 OSS 临时签名 URL，最长 7 天。
