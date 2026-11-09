# Tablestore 宽表模型速查（含踩坑记录）

> 对应文档：[快速使用宽表模型](https://help.aliyun.com/document_detail/27293.html)
> 本文是读完之后的落地版本，配合 `scripts/05_tablestore_demo.py` 一起看。

---

## 1. 三层结构

```
实例（Instance）         ← 计费、地域、网络在这里定
  └── 数据表（Table）    ← 只需定义主键
        └── 行（Row）    ← 主键 + 若干属性列
```

- **实例**：`cluster_type` 只有两种 —— `SSD`（高性能）、`HYBRID`（**容量型**）。建好不能改
- **表**：只定义主键和 TTL，属性列不用提前建
- **行**：一个主键 + 任意属性列，列可以在写入时临时出现

---

## 2. 本项目的表设计：`oss_documents`

用来索引 OSS 上的文档地址。

| 列 | 类型 | 角色 | 说明 |
| --- | --- | --- | --- |
| `id` | STRING | **主键（分区键）** | UUID，写入时生成 |
| `name` | STRING | 属性列 | 文档名，如 `hello.txt` |
| `url` | STRING | 属性列 | OSS 对象地址 |
| `describe` | STRING | 属性列 | 描述 |
| `category` | STRING | 属性列 | 分类，如 `demo` |
| `create_time` | INTEGER | 属性列 | 创建时间，**毫秒**时间戳 |

```python
TableMeta("oss_documents", [("id", "STRING")])
TableOptions(time_to_live=-1, max_version=1)      # 永不过期，保留 1 个版本
ReservedThroughput(CapacityUnit(0, 0))            # 0/0 = 按量付费
```

### 为什么 id 是字符串而不是自增整型

Tablestore 有条硬规则：**第一个主键（分区键）不能自增**，自增列只能出现在后面的主键上。
想用「整型自增 id」就必须多一个分区键列，例如：

```python
[("tenant", "STRING"), ("id", "INTEGER", PK_AUTO_INCR)]
```

这会多出一列、且 `id` 只在同一 `tenant` 内唯一。为了保持你要的 6 列结构，这里用字符串 UUID。

---

### 自增主键：`plans` 表的写法

`plans` 表把 `id` 放在第二位做自增：

```python
TableMeta("plans", [("uuid", "STRING"), ("id", "INTEGER", PK_AUTO_INCR)])
```

写入时 `id` 传 `PK_AUTO_INCR`，服务端回填；要拿到生成值需要 `return_type=ReturnType.RT_PK`：

```python
_, ret = client.put_row("plans", row, Condition(RowExistenceExpectation.IGNORE),
                        return_type=ReturnType.RT_PK)
auto_id = dict(ret.primary_key)["id"]
```

**自增出来的不是 1、2、3**，而是时间型的大整数（实测 `1794257563404000` → `1794257563485000`），
唯一且单调递增，适合排序，但别拿它当「第几条」。

## 3. 计费：容量型 + 按量付费

| 维度 | 本项目的选择 |
| --- | --- |
| 实例规格 | `HYBRID`（容量型），存储单价低，适合文档索引这类读少写少的场景 |
| 计费方式 | 按量付费 —— 预留读写吞吐量填 `CapacityUnit(0, 0)` |
| 表的 TTL | `-1` 永不过期；想自动清老数据就设成正数（秒） |

容量型实例只能按量付费，预留吞吐量必须为 0；设成非 0 建表会报错。

---

## 4. 常用操作

```python
from tablestore import *

# 建表
client.create_table(
    TableMeta("oss_documents", [("id", "STRING")]),
    TableOptions(time_to_live=-1, max_version=1),
    ReservedThroughput(CapacityUnit(0, 0)),
)

# 写（属性列直接带上，不存在就创建）
client.put_row("oss_documents",
               Row([("id", "abc123")], [("name", "hello.txt"), ("url", "https://...")]),
               Condition(RowExistenceExpectation.IGNORE))

# 读（指定主键）
consumed, row, token = client.get_row("oss_documents", [("id", "abc123")])
attrs = {k: v for k, v, _ts in row.attribute_columns}

# 扫描（范围查询，INF_MIN / INF_MAX 表示两端）
consumed, next_pk, rows, token = client.get_range(
    "oss_documents", "FORWARD",
    [("id", INF_MIN)], [("id", INF_MAX)], limit=10)

# 改（只 update 指定列）
client.update_row("oss_documents", Row([("id", "abc123")], {"put": [("category", "news")]}),
                  Condition(RowExistenceExpectation.EXPECT_EXIST))

# 删
client.delete_row("oss_documents", [("id", "abc123")], Condition(RowExistenceExpectation.IGNORE))
```

---

## 5. 踩坑记录（本机实测）

### 5.1 返回值是元组，不是对象

| 方法 | 返回 |
| --- | --- |
| `put_row` / `update_row` / `delete_row` | `(consumed, return_row)` |
| `get_row` | `(consumed, row, next_token)` |
| `get_range` | `(consumed, next_primary_key, rows, next_token)` |

写成 `res.consumed` 会直接 `AttributeError: 'tuple' object has no attribute 'consumed'`。

### 5.2 属性列是三元组

`row.attribute_columns` 里每项是 `(列名, 值, 时间戳)`，所以：

```python
dict(row.attribute_columns)                              # ❌ ValueError
{name: value for name, value, _ts in row.attribute_columns}   # ✅
```

封装成了 `tair.tablestore.row_to_dict(row)`。

### 5.3 update_row 的属性列是「操作字典」

```python
Row([("id", "abc")], {"put": [("category", "x")]})        # ✅
Row([("id", "abc")], [("category", "x")])                 # ❌ OTSClientError
Row([("id", "abc")], {"category": "x"})                   # ❌ OTSClientError
```

支持的键：`put`（写/覆盖）、`delete`（删指定版本）、`delete_all`（删整列）、`increment`（原子自增）。

### 5.4 主键列不能改

想改 `id` 只能删了重插；`update_row` 传主键值只会定位行，不会修改主键。

### 5.5 建实例时 NetworkTypeACL / NetworkSourceACL 要成对出现

只设一个会报 `Can not only set one param of NetworkTypeACL & NetworkSourceACL`。
两个都不设 = 默认全放开；要限制就两个都传。

### 5.6 实例状态字段是小写

`GetInstance` 返回的 `Status` 是 `normal` / `creating`，不是 `Normal`。轮询时注意大小写。

---

## 6. 在本仓库怎么跑

```bash
python scripts/05_tablestore_demo.py --list     # 只列表和表结构
python scripts/05_tablestore_demo.py --create   # 表不存在时自动建
python scripts/05_tablestore_demo.py            # 写入 → 读回 → 扫描 → 更新 → 删除
python scripts/05_tablestore_demo.py --keep     # 保留演示行
```

`.env` 配置：

```bash
OTS_INSTANCE=davine-dating
OTS_TABLE=oss_documents
# OTS_ENDPOINT=          # 留空按 https://{实例}.{地域}.ots.aliyuncs.com 拼
```

---

## 7. 和 Redis 怎么配合

文档索引表放「元数据」，正文放 OSS，Redis 只做热点缓存：

```
OSS       存文档正文（md/pdf/图片）
Tablestore 存 id / name / url / category / create_time（可检索、可翻页）
Redis      存「最近访问的 X 篇」这类热数据，命中就直接返回
```

查一篇文档的链路：`Tablestore 查元数据 → 拿 url → Redis 命中就回，没命中读 OSS 再回填`。
