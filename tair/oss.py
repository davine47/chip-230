"""OSS 封装（alibabacloud-oss-v2）。

对应文档：OSS Python SDK V2 —— https://help.aliyun.com/oss/developer-reference/2-0-manual-preview-version/
要点：
  - 凭证用 RAM 用户的 AK（这里直接取项目 .env，不依赖 OSS_ACCESS_KEY_ID 环境变量）
  - region 必填，endpoint 不填时 SDK 会按地域自动拼公网域名
  - 所有操作都是「Request 对象进、Response 对象出」，响应里有 status_code / request_id
"""

from __future__ import annotations

import sys
from urllib.parse import quote, unquote, urlsplit

import alibabacloud_oss_v2 as oss

from .config import Settings


def build_client(settings: Settings) -> oss.Client:
    config = oss.config.load_default()
    config.credentials_provider = oss.credentials.StaticCredentialsProvider(
        settings.access_key_id, settings.access_key_secret
    )
    config.region = settings.oss_region_or_default
    # endpoint 留空时 SDK 自己拼 oss-{region}.aliyuncs.com；要用内网/传输加速/自定义域名才需要填
    if settings.oss_endpoint:
        config.endpoint = settings.oss_endpoint
    config.connect_timeout = 5000  # 毫秒
    config.readwrite_timeout = 30000  # 毫秒，上传下载大对象时别太短
    return oss.Client(config)


_SHARED: list = []


def shared_client(settings: Settings) -> oss.Client:
    """进程内复用的客户端，别每个请求都重新建连接池。"""
    if not _SHARED:
        _SHARED.append(build_client(settings))
    return _SHARED[0]


def key_from_url(url: str) -> str:
    """从对象地址反推 key（URL 里的中文/空格是百分号编码的，要还原）。"""
    return unquote(urlsplit(url).path.lstrip("/"))


def bucket_names(client: oss.Client) -> list[dict]:
    """列出账号下所有 bucket，返回 [{'name','location','storage_class','created'}, ...]。"""
    result = client.list_buckets(oss.ListBucketsRequest())
    return [
        {
            "name": b.name,
            "location": b.location or "",
            "storage_class": b.storage_class or "",
            "created": b.creation_date or "",
        }
        for b in (result.buckets or [])
    ]


def guess_bucket(settings: Settings, client: oss.Client) -> str:
    """没在 .env 里指定 OSS_BUCKET 时，挑当前地域下唯一/第一个 bucket。

    返回空串表示挑不出来，调用方自己决定报错还是让用户传 --bucket。
    """
    if settings.oss_bucket:
        return settings.oss_bucket
    region_suffix = settings.oss_region_or_default
    candidates = [
        b["name"]
        for b in bucket_names(client)
        if b["location"] in (region_suffix, f"oss-{region_suffix}")
    ]
    return candidates[0] if candidates else ""


def object_url(settings: Settings, bucket: str, key: str) -> str:
    """拼一个对象的外网访问地址（只用于展示，不代表有访问权限）。"""
    endpoint = settings.oss_endpoint or f"oss-{settings.oss_region_or_default}.aliyuncs.com"
    return f"https://{bucket}.{endpoint}/{quote(key)}"


def read_body(result) -> bytes:
    """get_object 的 body 是老式流对象，统一读成 bytes。"""
    body = result.body
    return body.read() if hasattr(body, "read") else bytes(body)


def discard_object(client: oss.Client, bucket: str, key: str) -> None:
    """补偿删除：索引写失败时，把刚传上去的对象删掉，免得留下看板上看不到的孤儿。

    删除本身失败也不再抛（原始异常更重要），只打印出来，交给 scripts/06_reconcile.py 兜底。
    """
    try:
        client.delete_object(oss.DeleteObjectRequest(bucket=bucket, key=key))
    except Exception as exc:  # noqa: BLE001
        print(
            f"[warn] 回滚 OSS 对象失败，留下孤儿对象 {bucket}/{key}：{exc}"
            "（可运行 scripts/06_reconcile.py 清理）",
            file=sys.stderr,
        )
