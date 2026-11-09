#!/usr/bin/env python3
"""04_oss_demo.py —— OSS Python SDK V2 常用接口一路跑通。

对应文档：https://help.aliyun.com/oss/developer-reference/2-0-manual-preview-version/

覆盖：
    初始化客户端 / list_buckets / get_bucket_info / put_object / head_object /
    get_object / put_object_from_file / get_object_to_file / list_objects_v2 /
    copy_object / presign（预签名 URL）/ delete_object

用法：
    python scripts/04_oss_demo.py                 # 跑完整流程，结束自动清理
    python scripts/04_oss_demo.py --list-only     # 只读：列 bucket 和对象，不写不删
    python scripts/04_oss_demo.py --bucket davine-datanode-std
    python scripts/04_oss_demo.py --keep          # 保留写进去的对象

所有演示对象都写在 OSS_PREFIX（默认 chip-230-demo/）下，按次运行分目录，方便核对与清理。
"""

from __future__ import annotations

import argparse
import hashlib
import sys
import tempfile
import time
from datetime import timedelta
from pathlib import Path

import alibabacloud_oss_v2 as oss

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # 让脚本能 import tair 包

from tair import config
from tair import oss as oss_helper  # noqa: E402
from tair.console import line, section  # noqa: E402


def brief(result) -> str:
    """统一的响应摘要：状态码 + 请求 ID（排障要用）。"""
    code = getattr(result, "status_code", "?")
    rid = getattr(result, "request_id", "") or ""
    return f"status={code}  request_id={rid[:16]}…"


def main() -> int:
    parser = argparse.ArgumentParser(description="OSS SDK V2 接口演示")
    parser.add_argument("--bucket", help="指定 bucket，默认取 OSS_BUCKET 或当前地域第一个")
    parser.add_argument("--prefix", help="对象前缀，默认取 OSS_PREFIX")
    parser.add_argument("--keep", action="store_true", help="保留写入的对象，不删除")
    parser.add_argument("--list-only", action="store_true", help="只做只读操作")
    parser.add_argument("--clean", action="store_true", help="删除 OSS_PREFIX 下的演示对象后退出")
    args = parser.parse_args()

    settings = config.load_settings()
    if not settings.has_oss:
        print(config.MISSING_AK_HINT)
        return 1

    section("0. 初始化客户端")
    client = oss_helper.build_client(settings)
    line("Region", settings.oss_region_or_default)
    line(
        "Endpoint",
        settings.oss_endpoint or f"oss-{settings.oss_region_or_default}.aliyuncs.com（自动）",
    )
    line("凭证", settings.masked_ak())

    section("1. list_buckets —— 列出账号下的 bucket")
    try:
        buckets = oss_helper.bucket_names(client)
    except Exception as exc:  # noqa: BLE001
        print(f"[x] 失败: {type(exc).__name__}: {str(exc)[:200]}")
        return 1
    line("bucket 数量", len(buckets))
    for item in buckets:
        line(f"  {item['name']}", f"{item['storage_class']:<10} {item['location']}")

    bucket = args.bucket or oss_helper.guess_bucket(settings, client)
    if not bucket:
        print("\n[x] 没能确定要操作的 bucket，请用 --bucket 指定")
        return 1

    section(f"2. 目标 bucket：{bucket}")
    try:
        info = client.get_bucket_info(oss.GetBucketInfoRequest(bucket=bucket))
        line("get_bucket_info", brief(info))

        # v2 返回的是「结果对象 > bucket_info」两层，字段是 snake_case
        b = info.bucket_info
        line("  存储类型", b.storage_class)
        line("  数据地域", b.location)
        line("  ACL", b.acl)
        line("  创建时间", b.creation_date)
        line("  外网域名", b.extranet_endpoint)
        line("  内网域名", b.intranet_endpoint)
    except Exception as exc:  # noqa: BLE001
        print(f"[x] 取 bucket 信息失败: {type(exc).__name__}: {str(exc)[:200]}")
        return 1

    prefix = (args.prefix or settings.oss_prefix).rstrip("/") + "/"
    run = time.strftime("run-%Y%m%d-%H%M%S")
    root = f"{prefix}{run}"
    line("本次对象前缀", root + "/")

    if args.clean:
        section(f"清理 {bucket} 下前缀 {prefix} 的对象")
        removed, token = 0, ""
        while True:
            page = client.list_objects_v2(
                oss.ListObjectsV2Request(
                    bucket=bucket, prefix=prefix, max_keys=100, continuation_token=token
                )
            )
            for item in page.contents or []:
                client.delete_object(oss.DeleteObjectRequest(bucket=bucket, key=item.key))
                removed += 1
            if not page.is_truncated:
                break
            token = page.next_continuation_token
        line("已删除对象数", removed)
        return 0

    if args.list_only:
        section("3. list_objects_v2（只读模式，仅列出现有对象）")
        result = client.list_objects_v2(
            oss.ListObjectsV2Request(bucket=bucket, prefix=prefix, max_keys=10)
        )
        line("list_objects_v2", brief(result))
        line("  返回对象数", len(result.contents or []))
        line("  是否还有更多", result.is_truncated)
        for item in (result.contents or [])[:5]:
            line(f"  {item.key}", f"{item.size} bytes")
        return 0

    try:
        section("3. put_object —— 上传一段字符串（Markdown）")
        md = (
            "# OSS SDK V2 测试对象\n\n"
            f"- 生成时间：{time.strftime('%Y-%m-%d %H:%M:%S')}\n"
            "- 由 scripts/04_oss_demo.py 写入\n\n"
            "```bash\npip install alibabacloud-oss-v2\n```\n"
        ).encode()
        key_md = f"{root}/hello.md"
        put = client.put_object(oss.PutObjectRequest(bucket=bucket, key=key_md, body=md))
        line("put_object", brief(put))
        line("  ETag", put.etag)
        line("  对象地址", oss_helper.object_url(settings, bucket, key_md))

        section("4. head_object —— 只看元信息，不下载内容")
        head = client.head_object(oss.HeadObjectRequest(bucket=bucket, key=key_md))
        line("head_object", brief(head))
        line("  Content-Length", head.content_length)
        line("  Content-Type", head.content_type)  # SDK 会按后缀猜类型
        line("  Last-Modified", head.last_modified)

        section("5. get_object —— 把内容读回来")
        got = client.get_object(oss.GetObjectRequest(bucket=bucket, key=key_md))
        data = oss_helper.read_body(got)
        line("get_object", brief(got))
        line("  读到字节数", len(data))
        line("  与写入一致", data == md)

        section("6. put_object_from_file / get_object_to_file —— 文件流式上传与下载")
        local_md = Path("docs/ip-whitelist.md")
        if not local_md.is_file():
            local_md = Path(tempfile.gettempdir()) / "oss-demo-sample.md"
            local_md.write_text("# 临时样例\n\n文件上传测试\n", encoding="utf-8")
        raw = local_md.read_bytes()
        key_file = f"{root}/{local_md.name}"
        up = client.put_object_from_file(
            oss.PutObjectRequest(bucket=bucket, key=key_file), str(local_md)
        )
        line(f"上传 {local_md.name}", f"{len(raw)} bytes  {brief(up)}")

        download_to = Path(tempfile.gettempdir()) / f"oss-demo-{run}-{local_md.name}"
        client.get_object_to_file(
            oss.GetObjectRequest(bucket=bucket, key=key_file), str(download_to)
        )
        back = download_to.read_bytes()
        line(
            "下载后 sha256 一致",
            hashlib.sha256(back).hexdigest() == hashlib.sha256(raw).hexdigest(),
        )
        download_to.unlink(missing_ok=True)

        section("7. list_objects_v2 —— 按前缀列举")
        listed = client.list_objects_v2(
            oss.ListObjectsV2Request(bucket=bucket, prefix=root + "/", max_keys=100)
        )
        keys = [item.key for item in (listed.contents or [])]
        line("前缀下对象数", len(keys))
        for key in keys:
            line("  " + key.replace(root + "/", ""), "ok")
        line("是否截断（分页）", listed.is_truncated)

        section("8. copy_object —— 服务端拷贝，不经过本地")
        key_copy = f"{root}/hello-copy.md"
        copied = client.copy_object(
            oss.CopyObjectRequest(
                bucket=bucket, key=key_copy, source_bucket=bucket, source_key=key_md
            )
        )
        line("copy_object", brief(copied))
        line(
            "  副本大小",
            client.head_object(oss.HeadObjectRequest(bucket=bucket, key=key_copy)).content_length,
        )

        section("9. presign —— 生成带签名的临时下载链接")
        signed = client.presign(
            oss.GetObjectRequest(bucket=bucket, key=key_md), expires=timedelta(hours=1)
        )
        line("方法", getattr(signed, "method", "GET"))
        line("过期时间", getattr(signed, "expiration", "?"))
        line("URL", signed.url[:120] + "…")

        section("10. 清理")
        if args.keep:
            line("保留了对象（--keep）", root + "/")
            line("清理命令", f"python scripts/04_oss_demo.py --bucket {bucket} --clean")
        else:
            for key in keys + [key_copy]:
                client.delete_object(oss.DeleteObjectRequest(bucket=bucket, key=key))
            line("已删除对象数", len(keys) + 1)
            line(
                "残留检查",
                sum(
                    1
                    for _ in (
                        client.list_objects_v2(
                            oss.ListObjectsV2Request(bucket=bucket, prefix=root + "/")
                        ).contents
                        or []
                    )
                ),
            )
    except Exception as exc:  # noqa: BLE001
        status = getattr(exc, "status_code", "")
        print(f"\n[x] 执行中断: {type(exc).__name__} {status}: {str(exc)[:300]}")
        return 1

    print("\n全部接口调用完成。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
