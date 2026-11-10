# 阿里云 Tair（兼容 Redis）使用方法 + 可运行示例

> 参照两篇官方文档整理：
> 1. [命令支持范围与使用限制](https://help.aliyun.com/zh/redis/developer-reference/commands-supported-by-apsaradb-for-redis-community-edition) —— **数据面**：连上实例后能发哪些命令
> 2. [Python SDK 集成示例](https://help.aliyun.com/zh/redis/developer-reference/call-the-apsaradb-for-redis-api-by-using-alibaba-cloud-sdk-for-python) —— **控制面**：用 AccessKey 调 OpenAPI 管实例
>
> 项目内的速查：[白名单](docs/ip-whitelist.md) · [OSS SDK](docs/oss-quickstart.md) · [Tablestore](docs/tablestore-quickstart.md)

这两篇讲的其实是同一套东西的两个层面，实际项目里要配合使用：

```
   你的代码 / 运维脚本
        │
        ├── 控制面：HTTPS + AccessKey 签名  → OpenAPI（查实例、改白名单、变配…）
        │      产物：实例 ID、连接地址、端口、密码、白名单
        │
        └── 数据面：Redis 协议（RESP）      → 实例的 6379 端口（GET/SET/HSET…）
               前提：地址 + 端口 + 密码 + 白名单都对了
```

---

## 一、Redis 使用方法总结

### 1.1 数据面：连上实例的四要素

| 要素 | 从哪来 | 注意点 |
|---|---|---|
| 连接地址 | 控制台「连接信息」/ `DescribeInstanceAttribute` 的 `ConnectionDomain` | 默认是 **VPC 内网地址**，只有同 VPC 的 ECS 能连；本机调试要先申请**公网连接地址** |
| 端口 | 同上，`Port` | 默认 6379 |
| 密码 | 创建实例时设置，或 `DescribeAccounts` | 6.0 起支持自定义账号，需要「用户名 + 密码」两个都填 |
| 白名单 | 控制台「白名单设置」 | 默认只放行 `127.0.0.1`，不加你的出口 IP 一定连不上 |

客户端用官方的 **redis-py** 就行，阿里云只提供标准 Redis 协议，不需要专用 SDK：

```bash
pip install redis
```

```python
import redis

r = redis.Redis(
    host="r-xxxx.redis.rds.aliyuncs.com",
    port=6379,
    password="<实例密码>",
    decode_responses=True,      # 返回 str 而不是 bytes
    socket_timeout=5,
    health_check_interval=30,   # 复用空闲连接前先 PING，避免拿到已被服务端断开的连接
)
r.set("k", "v", ex=60)          # 带过期时间一起写，别 SET 完再 EXPIRE
print(r.get("k"), r.ttl("k"))
```

> 连接池：`redis.Redis` 内部自带连接池，**一个进程创建一个实例复用即可**，不要每次请求都 new。

### 1.2 命令族：全部支持的 16 类

官方命令表按命令族拆分，含义都是「该命令在 2.8 / 4.0 / 5.0 / 6.0 / 7.0 各版本是否支持」：

| 命令族 | 代表命令 | 典型用途 |
|---|---|---|
| String | `SET/GET/INCR/INCRBY/MSET/MGET/SETNX` | 缓存、计数器、分布式锁 |
| Hash | `HSET/HGET/HGETALL/HINCRBY/HDEL` | 对象字段局部读写 |
| List | `LPUSH/RPUSH/LRANGE/LTRIM/LPOP` | 队列、最新 N 条 |
| Set | `SADD/SMEMBERS/SINTER/SINTERSTORE/SCARD` | 标签、去重、交集 |
| Sorted Set | `ZADD/ZINCRBY/ZREVRANGE/ZRANK/ZCOUNT` | 排行榜、延迟队列 |
| Bitmap | `SETBIT/GETBIT/BITCOUNT/BITOP/BITFIELD` | 签到、布尔标记 |
| HyperLogLog | `PFADD/PFCOUNT/PFMERGE` | UV 去重计数（固定约 12KB） |
| Geospatial | `GEOADD/GEODIST/GEOSEARCH` | 附近的人 / 距离计算 |
| Stream | `XADD/XREAD/XGROUP/XACK/XLEN` | 消息队列（5.0+） |
| Pub/Sub | `PUBLISH/SUBSCRIBE/PSUBSCRIBE` | 广播通知（不持久化） |
| Transactions | `MULTI/EXEC/DISCARD/WATCH` | 事务 |
| Scripting | `EVAL/EVALSHA/FCALL` | Lua 原子逻辑 |
| Connection | `AUTH/PING/CLIENT SETNAME/CLIENT LIST` | 连接管理 |
| Generic | `DEL/EXISTS/EXPIRE/TTL/SCAN/TYPE` | Key 通用操作 |
| Server | `INFO/CONFIG GET/DBSIZE/FLUSHDB` | 运维（部分受限） |
| Cluster | `CLUSTER INFO/NODES/SLOTS/KEYSLOT` | 集群信息（集群架构才用） |

### 1.3 版本差异：照着表选版本，别照着本地 Redis 写代码

- **5.0** 起：`Stream`（XADD/XREAD…）、`ZPOPMIN/ZPOPMAX`、`BZPOPMIN`
- **6.0** 起：`BITFIELD_RO`、`CLIENT CACHING`、`CLIENT TRACKING`、`CLIENT GETREDIR`、`LPOS`
- **7.0** 起：`GETDEL`、`GETEX`、`LCS`、`COPY`、`SINTERCARD`、`SMISMEMBER`、`GEOSEARCH`、`BZMPOP`、`XAUTOCLAIM`、`FUNCTION LOAD/FCALL`、`EVAL_RO`

如果你的实例是 5.0 或 6.0，用了 7.0 才有的命令会直接报 `ERR unknown command`。写代码前先在表里查一眼。

### 1.4 架构差异：这是最容易踩坑的地方

| | 标准架构（主从） | 集群架构（Proxy 地址） | 集群架构（直连地址） | 读写分离 |
|---|---|---|---|---|
| 多 Key 命令（`SINTER`/`ZUNIONSTORE`/事务） | 随便用 | **要求同一个 Slot**（或走 Proxy 支持的少量跨 Slot 命令，如 `DEL`/`EXISTS`） | 严格要求同一 Slot | 同 Proxy |
| `SELECT` | 支持 | 支持（Proxy 屏蔽细节） | 支持，但部分客户端在集群初始化模式下不支持 | 支持 |
| Cluster 系列命令 | ❌ 不适用 | 仅 `CLUSTER INFO/KEYSLOT/NODES/SLAVES/SLOTS` | 部分命令可用 | 同 Proxy |
| `SCAN` | 全库遍历 | 从首个分片开始按序扫全部分片（只想扫某个分片用 `ISCAN`） | 只扫当前节点 | 同 Proxy |
| 事务 | 完整语义 | 同一个 Slot 内才有事务语义 | 严格同 Slot | 同 Proxy |

**Hash Tag**：要想让一批 Key 落在同一个 Slot，用 `{}` 包一个相同的前缀：

```
{tair:demo}:tag:keyboard
{tair:demo}:tag:gaming      ← 和上面同一个 Slot，SINTER 才能跑
```

本项目的所有演示 Key 都用 `{tair:demo}` 前缀，所以标准/集群/读写分离架构都能直接跑。

### 1.5 明确的限制（官方表里标 ❌ 或特殊说明的）

- `MIGRATE`：所有版本都不支持
- `CONFIG SET`：**只返回 `OK`，不会真正改参数**（为了兼容某些客户端框架）。要改参数用控制台或 `ModifyInstanceConfig`
- `ACL` 系列：绝大多数不支持（`ACL WHOAMI` 仅 7.0 支持）
- 部分命令需要**开通直连访问**并用直连地址连接（表里带 ① 的命令，例如 `CLUSTER INFO`）
- `SWAPDB`：仅兼容 Redis 7.0 的集群架构实例支持
- 集群架构只有 **db 0**：`SELECT` 命令虽然被接受，但切不了库，业务里别依赖「多 db 隔离」

### 1.6 生产使用建议（经验性，非文档原文）

1. **一定设 TTL**：缓存类 Key 不带过期时间，迟早把内存打满
2. **`SCAN` 代替 `KEYS`**：`KEYS` 是 O(N) 且阻塞实例
3. **Pipeline 批量**：一次网络往返发多条命令，脚本里见 `demo_pipeline`
4. **分布式锁用 `SET key val NX EX ttl`**，释放用 Lua 校验 token（防止误删别人的锁）
5. **控制单 Key 大小**：大 Key（几 MB 的 Hash/List）会拖慢整个实例
6. **不用 `FLUSHALL`/`FLUSHDB` 做测试清理**，用 `SCAN` + `DEL` 按前缀删
7. **查看慢查询**：控制台慢日志，或 `DescribeSlowLogRecords` 接口

---

## 二、控制面：Python SDK 调 OpenAPI

### 2.1 准备身份与权限（一次性）

1. RAM 控制台创建子账号，访问方式选「使用永久 AccessKey」
2. 给它附加 **`AliyunKvstoreReadOnlyAccess`**（只读，够本文演示）；要改配置则用 `AliyunKvstoreFullAccess`
3. 保存 AccessKey ID / Secret —— **只显示一次**

> 千万别用主账号 AK，泄露等于整个账号被接管。

### 2.2 装包 + 配 AK

```bash
pip install alibabacloud_credentials alibabacloud_r_kvstore20150101 alibabacloud_tea_console

export ALIBABA_CLOUD_ACCESS_KEY_ID=<access_key_id>
export ALIBABA_CLOUD_ACCESS_KEY_SECRET=<access_key_secret>
```

### 2.3 调用套路：永远是这四步

```python
from alibabacloud_tea_openapi import models as open_api_models
from alibabacloud_tea_util import models as util_models
from alibabacloud_r_kvstore20150101.client import Client
from alibabacloud_r_kvstore20150101 import models as kv_models

# ① 配置（AK + 地域）
config = open_api_models.Config(
    access_key_id=settings.access_key_id,
    access_key_secret=settings.access_key_secret,
    region_id="cn-hangzhou",
)

# ② 客户端
client = Client(config)

# ③ 请求对象（业务参数都在这里）
request = kv_models.DescribeInstancesOverviewRequest(region_id="cn-hangzhou")

# ④ 发起调用 + 取 body
response = client.describe_instances_overview_with_options(request, util_models.RuntimeOptions())
print(response.status_code, response.body.total_count)
for ins in response.body.instances:
    print(ins.instance_id, ins.instance_status, ins.connection_domain)
```

**两个官方文档里没写、但一定会踩的坑：**

1. **endpoint 不用手写**。SDK 会按 `RegionId` 自动解析（`cn-hangzhou` → `r-kvstore.aliyuncs.com`）。
   手动写 `r-kvstore.cn-hangzhou.aliyuncs.com` 这种地域化域名反而**解析不到**（本机实测 DNS 失败）。
   只有走代理/专线时才用 `config.endpoint` 覆盖。
2. **返回字段是 snake_case**（`body.instances[0].connection_domain`），不是文档 JSON 里的 `ConnectionDomain`。
   想拿文档里那种 PascalCase 结构，调用 `.to_map()`。

### 2.4 常用接口速查

| 场景 | 接口 | 权限 |
|---|---|---|
| 查实例列表（本文示例） | `DescribeInstancesOverview` | 只读 |
| 分页查实例（实例多时用） | `DescribeInstances`（`PageNumber`/`PageSize`） | 只读 |
| 查单个实例详情 / 连接地址 | `DescribeInstanceAttribute` | 只读 |
| 查网络地址（内网/公网/直连） | `DescribeDBInstanceNetInfo` | 只读 |
| 查 / 改白名单 | `DescribeSecurityIps` / `ModifySecurityIps` | 只读 / 读写 |
| 申请公网地址 | `AllocateInstancePublicConnection` | 读写 |
| 查账号 / 改密码 | `DescribeAccounts` / `ModifyAccountPassword` | 只读 / 读写 |
| 监控、慢日志 | `DescribeHistoryMonitorValues`、`DescribeSlowLogRecords` | 只读 |
| 创建、重启、变配 | `CreateInstance`、`RestartInstance`、`ModifyInstanceSpec` | 读写 |

---

## 三、示例项目

### 目录结构

```
chip-230/
├── tair/                         公共库（脚本和看板共用）
│   ├── __init__.py
│   ├── config.py                 配置中心：AK 与各云资源连接信息（.env / 环境变量）
│   ├── openapi.py                控制面封装：SDK 客户端 + 查询接口 + 错误翻译
│   ├── redis_conn.py             数据面封装：连接池、TLS、超时、健康检查
│   ├── oss.py                    OSS 封装：客户端、bucket 选择、对象地址
│   ├── tablestore.py             Tablestore 封装：实例客户端、Row 转 dict
│   ├── docs.py                   文档库：OSS 上传 + 索引（documents / davine 共用）
│   ├── plans.py                  Plan 库：.md 上传 + plans 表（uuid + 自增 id）
│   └── console.py                终端输出对齐小工具
├── scripts/                      命令行示例
│   ├── 00_doctor.py              一键自检：依赖 / AK / OpenAPI / Redis 连通性
│   ├── 01_openapi_list_instances.py  对应文档示例：查询实例列表（--raw 打印原始 JSON）
│   ├── 02_redis_commands.py      数据面实战：16 类命令族跑一遍（商品页场景）
│   ├── 03_end2end.py             AK 查实例 → 拿地址 → 连上去读写，完整链路
│   ├── 04_oss_demo.py            OSS SDK V2：上传 / 下载 / 列举 / 拷贝 / 预签名
│   ├── 05_tablestore_demo.py     表格存储：建表 / 写入 / 查询 / 扫描 / 更新 / 删除
│   └── allow_my_ip.py            把本机出口 IP 加进白名单（IP 变了连不上时跑）
├── web/                          看板（Flask）
│   ├── app.py                    页面路由 + JSON 接口
│   ├── templates/                base / index / redis / instance / commands / docstore / plans
│   └── static/                   style.css / app.js / docstore.js / plans.js
├── docs/                         三份速查文档
│   ├── ip-whitelist.md           白名单配置完整步骤
│   ├── oss-quickstart.md         OSS SDK V2 速查与踩坑
│   └── tablestore-quickstart.md  表格存储宽表模型速查与踩坑
├── run.md                        看板启动/停止/排错
├── requirements.txt              运行依赖
├── pyproject.toml                ruff 代码风格配置（可选，仅开发用）
├── .env.template                 配置模板（复制成 .env 再填，模板里不含真实密钥）
├── .gitignore                    .env 已被忽略，不会误提交
└── LICENSE                       Apache License 2.0
```

### 快速开始

```bash
# 1. 装依赖（建议虚拟环境）
python3 -m venv .venv && source .venv/bin/activate     # Windows: .venv\Scripts\activate
pip install -r requirements.txt

# 2. 填 AccessKey（你之后要配的就是这一步，只影响 OpenAPI）
cp .env.template .env
#   编辑 .env：ALIBABA_CLOUD_ACCESS_KEY_ID / ALIBABA_CLOUD_ACCESS_KEY_SECRET

# 3. 自检 + 跑起来
python scripts/00_doctor.py                # 全绿就可以继续
python scripts/01_openapi_list_instances.py          # 控制面：看实例列表
python scripts/01_openapi_list_instances.py --raw    # 打印文档里那种完整 JSON

# 4. 想验证数据面：把实例地址和密码也填进 .env
#    REDIS_HOST=...  REDIS_PASSWORD=...
#    连不上先加白名单：./.venv/bin/python scripts/allow_my_ip.py
#    （九成是出口 IP 不在白名单，详见 docs/ip-whitelist.md）
python scripts/02_redis_commands.py        # 跑一遍常用命令（--keep 保留数据）
python scripts/03_end2end.py               # 端到端：查实例 → 连接 → 读写验证
```

### 02 脚本的输出长这样

```
====================================================================
 5. ZSet —— 销量排行榜（有序集合）
====================================================================
    ZINCRBY 累加销量                        3
    ZREVRANGE 0 2 WITHSCORES（Top3）        [('1002', 5.0), ('1001', 3.0), ('1003', 1.0)]
    ZSCORE 1002                             5.0
    ZREVRANK 1002（从 0 开始的排名）        0
    ZCOUNT 3 ~ +inf                         2
```

每个函数对应一个命令族，函数名就是 `demo_<命令族>`，想单独试某条命令直接改参数跑即可。

> 注意：**02 不需要 AccessKey**。数据面只认「地址 + 端口 + 密码 + 白名单」，
> AccessKey 只用于控制面（01 / 03）。所以你可以先把 02 在本地/ECS 上跑通，再配 AK。

### Web 看板（可选）

`web/` 下是一个 Flask 看板，视觉风格对齐 `~/gugugaga`：浅色渐变底、白色圆角卡片、顶部彩色渐变条。

```bash
./.venv/bin/python web/app.py                 # http://127.0.0.1:8080
WEB_PORT=8090 ./.venv/bin/python web/app.py   # 换端口
```

启动、后台运行、停止和常见问题都写在 **[run.md](run.md)** 里。

首页把四块看板画成一块芯片版图：**Redis**、**文档库**、**davine**、**Plan 库** 各占一块功能区，
下面是「最近的改动」（三个库合并按时间倒序，服务端渲染，带缓存）。
Redis 点进去是二级看板（键空间 / 实例 / 命令，用子导航切换），其余三块各自是一个独立页面。

| 路径 | 层级 | 内容 |
| --- | --- | --- |
| `/` | 顶级 | 四块功能区（数量 / 最近一份）+ 最近的改动 |
| `/redis` | Redis 二级 | **键空间**：SCAN 搜索、点行看值、改值、设 TTL、删除 |
| `/redis/instance` | Redis 二级 | 实例信息（OpenAPI）与连接信息，只读 |
| `/redis/commands` | Redis 二级 | 16 个命令族、版本差异、限制清单 |
| `/documents` | 顶级 | **文档库**：上传文件到 OSS `agents/docs/`，同时写 Tablestore 索引 |
| `/davine` | 顶级 | **davine**：同一套逻辑，文件进 `davine_exchange/`，索引写 `davine_oss_documents` |
| `/plans` | 顶级 | **Plan 库**：只收 `.md`，传 OSS `plans/`，写 Tablestore `plans` 表 |

> 早先的 `/instance`、`/commands` 会 301 跳到 `/redis/` 下对应的新路径。

#### 文档库 `/documents` 与 `/davine`

两块看板共用同一套实现（`tair.docs.DocumentStore`），只是「前缀 + 表名」不同：

| 看板 | OSS 前缀 | Tablestore 表 |
| --- | --- | --- |
| `/documents` | `agents/docs/` | `oss_documents` |
| `/davine` | `davine_exchange/` | `davine_oss_documents` |

表结构基本一致：主键 `id`(STRING)，属性列 `name / url / describe / category / create_time`；
davine 额外有 `tmp_public_url`（上传时签好的临时下载地址，见下）。

两块的差别：

- `category`：文档库自由输入；davine 是固定下拉（`others / paper / books / agents`），后端按枚举校验
- `description`：文档库必填；davine 可选（不填就不写这一列，列表里显示 `-`）
- **单文件上限**：文档库 25MB；davine 512MB
- **上传进度**：davine 传大文件时显示进度条（浏览器传完 → 条纹动画表示「正在写入 OSS」）
- **临时链接**：davine 上传成功后直接返回一条签名下载地址，有效期在上传时选
  （1 小时 / 6 小时 / 24 小时 / 3 天 / 7 天，默认 24 小时）

大文件走的是「落临时文件 → OSS uploader 分片上传」，内存占用与文件大小无关。

davine 的上传响应会多三个字段：`temporary_url`（签名地址）、`expires_at`（绝对过期时间）、
`expires_hours`（有效期小时数）。桶是私有的，这个地址在有效期内可以直接下载，过期后 403。
签名 URL 最长 7 天，超过这个范围会在签发前被拒。

这条链接同时写进索引表的 `tmp_public_url` + `tmp_expire_time` 两列，所以**刷新页面不会丢**：
列表每行末尾有「临时链接」按钮，点开在该行下面展开地址（可复制/打开）；没有链接的老数据显示
「无临时链接」，已过期的显示「临时链接已于 xx 过期」并不再返回地址。

接口是 `GET /api/davine/<id>/tmp-link`。两列只在按 id 查单条时读取，**列表扫描不返回它们**，
免得打开页面就把所有签名链接发给浏览器。过期数据按需求不清理，仍留在表里。

上传一次做两件事：

1. 文件写进 OSS：`oss://<bucket>/agents/docs/<文件名>`（同名自动加时间戳，不覆盖）
2. 往 Tablestore 的 `oss_documents` 表插一行：`id / name / url / describe / category / create_time`

页面要求填 **category** 和 **description**，列表按创建时间倒序，支持「打开」（临时生成
OSS 预签名链接，桶是私有的）、「删除」（同时删索引行和 OSS 对象）。

接口：

| 方法 | `/api/docs`（文档库） | `/api/davine`（davine） |
| --- | --- | --- |
| GET | 列出全部文档 | 同左 |
| POST `/upload` | multipart：`file` + `category` + `description`，上限 25MB | 同左，`description` 可选，上限 512MB |
| POST `/<id>/link` | 生成 1 小时预签名地址 | 同左 |
| DELETE `/<id>` | 删索引 + 删 OSS 对象 | 同左 |

> davine 的 `POST /upload` 与 `POST /complete` 支持 `expire_hours` 参数，成功响应额外带
> `temporary_url` / `expires_at` / `expires_hours`；文档库没有这个参数，响应也不含这些字段。

> 写接口（POST/PUT/DELETE）必须带 `X-Requested-With: chip-230`，否则返回 403。
> 这是防 CSRF 的：页面里的 JS 会自动带，自己用 curl 调要手动加。

#### Plan 库 `/plans`

只接受 `.md`；**github_project_name / github_project_branch 必填**，`plan_info`、`github_project_url`、
`plan_name`、`trace_id`、`trace_info` 都可选。

| 项 | 值 |
| --- | --- |
| OSS 路径 | `oss://<bucket>/plans/<plan_name>.md`，重名自动加时间戳 |
| 表 | Tablestore `plans` |
| 主键 | `uuid`（STRING，分区键）+ `id`（INTEGER，**自增**） |
| 属性列 | `plan_name` / `trace_id` / `trace_info` / `plan_info` / `create_time` / `oss_url` / `github_project_name` / `github_project_url` / `github_project_branch` |

```bash
curl -X POST http://127.0.0.1:8080/api/plans/upload \
  -H "X-Requested-With: chip-230" \
  --form-string "plan_info=计划摘要" \
  --form-string "github_project_name=chip-230" \
  --form-string "github_project_branch=main" \
  --form-string "github_project_url=https://github.com/davine47/chip-230" \
  -F "file=@plan.md"
```

`github_project_name` 和 `github_project_branch` 必填；`plan_info`、`github_project_url` 可选，
为空时都不写对应的列，列表里显示 `—`。

> 自增 id 不是 1、2、3，而是**时间型的 16 位整数**（如 `1794257644716000`），
> 由服务端生成，保证唯一且单调递增，可以直接用来排序。
>
> `oss_url` 存完整对象地址；预签名和删除时从 URL 反推 key，计划名带时间戳也不会找错。

Key 管理页的几个细节：

- 列表用 `SCAN` + `pipeline` 批量取 `TYPE`/`TTL`，不用 `KEYS`
- String 可直接编辑；Hash/List/Set/ZSet 只读预览，避免误覆盖结构
- 超过 10 万字符的值用 `GETRANGE` 截断读取，不会把大 value 拉进内存
- 支持深链：`/redis?key=davine` 直接打开编辑抽屉，`/redis?pattern={tair:demo}:*` 预填搜索

> ⚠️ 这个服务能直接改线上实例的数据，默认只监听 `127.0.0.1`。
> 不要把 `WEB_HOST` 改成 `0.0.0.0` 暴露出去；给同事用得先加一层认证。

---

## 四、常见报错对照表

| 报错 | 原因 | 处理 |
|---|---|---|
| `InvalidAccessKeyId.NotFound` | AK ID 错、有空格、或不是这个账号的 | 重新复制；确认没用主账号/子账号混了 |
| `SignatureDoesNotMatch` | AK Secret 错 | 重新生成 Secret |
| `Forbidden.RAM` / `NoPermission` | RAM 没授权 | 附加 `AliyunKvstoreReadOnlyAccess` |
| `InvalidRegionId.NotFound` | 地域写错 | 例如 `cn-hangzhou`、`cn-beijing` |
| `NOAUTH Authentication required` | 没传密码 | 填 `REDIS_PASSWORD` |
| `WRONGPASS` | 密码/用户名错 | 自定义账号要同时填 `REDIS_USERNAME` |
| `Connection refused` / `timed out` | 白名单没放行 / 用了内网地址 / 安全组 | 加白名单；本机改用公网地址；**出口 IP 变了就跑 `python scripts/allow_my_ip.py`** |
| `CROSSSLOT Keys in request don't hash to the same slot` | 集群架构跨 Slot 多 Key 操作 | 给 Key 加相同 Hash Tag |
| `MOVED` / `ASK` 重定向 | 用单机客户端连了集群直连地址 | 换 Proxy 地址，或用 `RedisCluster` 客户端 |
| `ERR unknown command` | 版本不支持该命令 | 对照命令支持表，或升级大版本 |

---

## 五、安全提醒

- `.env` 已在 `.gitignore` 里，**不要把 AK 写进代码或提交到 Git**
- 用 RAM 子账号 + 最小权限策略，不要用主账号 AK
- 打印日志时脱敏（本项目 `Settings.masked_ak()` 就是这么做的）
- AK 定期轮换；云上 ECS 场景优先用**实例 RAM 角色**（免 AK）
- 生产环境建议开启 **TLS 加密连接**（`REDIS_SSL=true`）并配置 CA 校验

---

## 六、License

[Apache License 2.0](LICENSE)

## 七、白名单（换机器 / IP 变了看这份）

家宽/办公网的公网出口 IP 会变，一变就会被实例白名单挡住，表现为 `Timeout connecting to server`、
看板一直转圈。一条命令修好：

```bash
python scripts/allow_my_ip.py          # 自动探测当前出口 IP，写进专用分组 dev_auto（覆盖该分组）
python scripts/allow_my_ip.py --show   # 只看当前白名单
```

需要 AccessKey 具备改白名单的权限（`AliyunKvstoreFullAccess`，或只给
`kvstore:ModifySecurityIps` 的最小策略）。

完整的步骤——控制台怎么点、API 怎么调、规则怎么写、换机器时的检查清单、排查表——
都整理在 **[docs/ip-whitelist.md](docs/ip-whitelist.md)**，可以脱离本仓库单独照着做。

---

## 八、对象存储 OSS（可选）

Redis 之外，仓库里还带了一份 OSS SDK V2 的示例（`scripts/04_oss_demo.py`），
和 Redis 那套共用同一个 AccessKey，只是多装一个包。

```bash
pip install alibabacloud-oss-v2

# 只读：列 bucket、看桶信息、列对象
python scripts/04_oss_demo.py --list-only

# 完整流程：上传 → head → 读回 → 文件上传/下载 → 列举 → 拷贝 → 预签名 → 删除
python scripts/04_oss_demo.py
```

`.env` 里新增这四项（都可留空，有默认行为）：

```bash
OSS_BUCKET=davine-datanode-std     # 不填则取当前地域第一个 bucket
OSS_REGION=cn-beijing              # 不填沿用 ALIBABA_CLOUD_REGION_ID
# OSS_ENDPOINT=oss-cn-beijing.aliyuncs.com
OSS_PREFIX=chip-230-demo/          # 演示对象都写在这个前缀下
```

演示只在 `OSS_PREFIX` 下按次生成目录，跑完自动删干净（`--keep` 可保留）。
接口用法、Result 的两层结构、`presign`、分片上传、最小权限策略这些细节，
见 **[docs/oss-quickstart.md](docs/oss-quickstart.md)**。

---

## 九、表格存储 Tablestore（可选）

`scripts/05_tablestore_demo.py` 演示宽表模型：建表、写入、按主键查、范围扫描、更新、删除。
表 `oss_documents` 用来索引 OSS 上的文档地址；`davine_oss_documents` 是 davine 看板用的同构表。

| 列 | 类型 | 角色 |
| --- | --- | --- |
| `id` | STRING | 主键（分区键），UUID |
| `name` | STRING | 属性列，文档名 |
| `url` | STRING | 属性列，OSS 对象地址 |
| `describe` | STRING | 属性列，描述 |
| `category` | STRING | 属性列，分类 |
| `create_time` | INTEGER | 属性列，毫秒时间戳 |

```bash
python scripts/05_tablestore_demo.py --list     # 只列表和表结构
python scripts/05_tablestore_demo.py            # 写入 → 读回 → 扫描 → 更新 → 删除
python scripts/05_tablestore_demo.py --keep     # 保留演示行
```

实例是**容量型（HYBRID）+ 按量付费**（`ReservedThroughput(0, 0)`），地域 `cn-beijing`。
`plans` 表则在 `uuid`（分区键）之外多了一个**自增主键** `id`，见上文 Plan 库。

> 注意：Tablestore 的**第一个主键（分区键）不能自增**，所以 `id` 用了字符串 UUID；
> 想要整型自增得额外加一个分区键列，详见 [docs/tablestore-quickstart.md](docs/tablestore-quickstart.md)。
