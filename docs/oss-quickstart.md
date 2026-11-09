# OSS Python SDK V2 速查（含踩坑记录）

> 对应文档：[OSS Python SDK V2](https://help.aliyun.com/oss/developer-reference/2-0-manual-preview-version/)
> 本文是读完之后的可执行版本，配合 `scripts/04_oss_demo.py` 一起看。

---

## 1. 先分清 V1 / V2

| | V1 | V2（本文） |
| --- | --- | --- |
| 包名 | `oss2` | `alibabacloud-oss-v2` |
| 风格 | `bucket.put_object(key, data)` 方法挂在 bucket 上 | `client.put_object(PutObjectRequest(...))`，请求对象进、结果对象出 |
| 命名 | snake_case | snake_case，但**结构是「Result > 子对象」两层**，见第 6 节 |

网上多数老文章是 V1，别照抄。

```bash
pip install alibabacloud-oss-v2
python -c "import alibabacloud_oss_v2 as oss; print('ok')"
```

---

## 2. 凭证

文档推荐用环境变量 + RAM 用户：

```bash
export OSS_ACCESS_KEY_ID='...'
export OSS_ACCESS_KEY_SECRET='...'
```

```python
credentials_provider = oss.credentials.EnvironmentVariableCredentialsProvider()
```

本项目没有用环境变量，而是直接读 `.env` 里的 AK，构造静态凭证（少一套变量，脚本更好带）：

```python
from tair import config, oss as oss_helper
client = oss_helper.build_client(config.load_settings())
```

其他凭证方式（文档里都有）：`StaticCredentialsProvider`、STS、`RAMRoleArn`、`EcsRamRole`、
`OidcRoleArn`、自定义 Provider、`AnonymousCredentialsProvider`（匿名只读公开桶）。

---

## 3. 初始化客户端

```python
import alibabacloud_oss_v2 as oss

cfg = oss.config.load_default()
cfg.credentials_provider = provider
cfg.region = "cn-beijing"          # 必填
cfg.endpoint = "oss-cn-beijing.aliyuncs.com"   # 可不填，SDK 按 region 自动拼
cfg.connect_timeout = 5000         # 毫秒
cfg.readwrite_timeout = 30000      # 毫秒
client = oss.Client(cfg)
```

- **不填 endpoint**：按 `oss-{region}.aliyuncs.com` 拼公网域名（推荐）
- **内网**：`oss-cn-beijing-internal.aliyuncs.com`，同地域 ECS 用这个不走公网流量
- **传输加速**：`oss-accelerate.aliyuncs.com`
- 异步客户端：`oss.AsyncClient(cfg)`，方法名前缀相同，用 `await`

---

## 4. 调用套路：三步

```python
result = client.put_object(                   # ③ 发请求，拿到 Result
    oss.PutObjectRequest(                     # ② 组装请求对象
        bucket="my-bucket", key="a/b.md", body=b"hello"
    )
)
print(result.status_code, result.request_id, result.etag)   # ① 从 Result 取结果
```

响应里 `request_id` 一定要打出来——提工单时阿里云只认这个。

---

## 5. 常用接口

| 分类 | 方法 | 说明 |
| --- | --- | --- |
| Bucket | `list_buckets` / `get_bucket_info` / `is_bucket_exist` | 列举、详情、存在性 |
| Bucket 写 | `put_bucket` / `delete_bucket` | 创建/删除（名字全局唯一，删除需桶为空） |
| 对象写 | `put_object` | body 传 bytes |
| 对象写 | `put_object_from_file` | 传本地路径，流式上传，大文件用这个 |
| 对象读 | `get_object` / `get_object_to_file` | 前者 `result.body.read()`，后者直接落盘 |
| 元信息 | `head_object` | 只取大小/ETag/Content-Type，不下载内容 |
| 列举 | `list_objects_v2` | 支持 `prefix` / `max_keys` / `continuation_token` 分页 |
| 拷贝 | `copy_object` | 服务端拷贝，`source_bucket` + `source_key` |
| 删除 | `delete_object` / `delete_multiple_objects` | 单个 / 批量 |
| 预签名 | `presign(GetObjectRequest(...), expires=timedelta(hours=1))` | 生成临时 URL，不开桶权限也能给别人下载 |
| 大文件 | `uploader` / `downloader` | 分片断点续传，内部自动 Multipart |
| 分片 | `initiate_multipart_upload` / `upload_part` / `complete_multipart_upload` | 手写分片时用 |

---

## 6. 踩坑记录（都是本机实测的）

### 6.1 Result 是两层结构，不是一层

```python
info = client.get_bucket_info(oss.GetBucketInfoRequest(bucket="xxx"))
info.bucket_info.storage_class      # ✅
info.storage_class                  # ❌ AttributeError
info.bucket                         # ❌ AttributeError，字段名是 bucket_info
```

同类：`list_buckets()` 的结果是 `result.buckets`（列表），`list_objects_v2()` 是 `result.contents`。

### 6.2 这个版本的 Client 没有 close()

```python
client.close()   # ❌ AttributeError: 'Client' object has no attribute 'close'
```

文档示例里有，但当前 PyPI 版本没这方法，直接用即可（GC 会回收连接池）。

### 6.3 get_object 拿到的是流，不是 bytes

```python
body = result.body.read()          # 或者封装成 tair.oss.read_body(result)
```

### 6.4 Content-Type 自动猜

`put_object` 不传 `content_type` 时按后缀推断：`.md` → `text/markdown`，`.json` → `application/json`。
Web 端按 Content-Type 渲染，所以后缀别乱写。

### 6.5 预签名 URL 的过期时间是 UTC

`presign(..., expires=timedelta(hours=1))` 返回的 `expiration` 是 UTC 时间，别以为算错了 8 小时。

---

## 7. 权限

RAM 用户默认没有 OSS 权限。两种策略：

- `AliyunOSSFullAccess`：读写都行，图省事就用它
- 最小权限：只给某个 bucket 的指定前缀

```json
{
  "Version": "1",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": ["oss:GetObject", "oss:PutObject", "oss:ListObjects"],
      "Resource": ["acs:oss:*:*:my-bucket/chip-230-demo/*"],
      "Condition": {}
    },
    {
      "Effect": "Allow",
      "Action": ["oss:ListBuckets", "oss:GetBucketInfo"],
      "Resource": ["acs:oss:*:*:*"]
    }
  ]
}
```

---

## 8. 在本仓库怎么跑

```bash
# 只读：列 bucket、看桶信息、列对象
python scripts/04_oss_demo.py --list-only

# 完整流程：上传 / 下载 / 拷贝 / 预签名 / 删除（结束自动清理）
python scripts/04_oss_demo.py

# 指定桶和前缀
python scripts/04_oss_demo.py --bucket my-bucket --prefix test/ --keep
```

`.env` 里相关配置：

```bash
OSS_BUCKET=davine-datanode-std     # 不填则取当前地域第一个桶
OSS_REGION=cn-beijing              # 不填沿用 ALIBABA_CLOUD_REGION_ID
# OSS_ENDPOINT=oss-cn-beijing.aliyuncs.com
OSS_PREFIX=chip-230-demo/          # 演示对象都写在这个前缀下
```

---

## 9. 什么时候别用 OSS SDK

- 只是给前端发个下载链接 → 后端 `presign` 一次即可，不用把 AK 给前端
- 要加速静态资源 → 挂 CDN，不是 SDK 的事
- 要在浏览器直传 → 用 STS 临时凭证 + 表单上传，别把永久 AK 打进前端
