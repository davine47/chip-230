#!/usr/bin/env python3
"""06_reconcile.py —— OSS 与 Tablestore 索引对账。

文件存 OSS、索引存 Tablestore，两边不在一个事务里。代码已经用「顺序 + 补偿」尽量避免不一致，
这个脚本负责兜底，找出两类问题：

    孤儿对象  OSS 前缀下有对象，但索引表里没有任何一行指向它（看板列不出来，也删不掉，一直收费）
    悬空索引  索引表里有行，但指向的 OSS 对象已经不在了（看板上能看到，点开 404）

用法：
    python scripts/06_reconcile.py                    # 只读：列出问题，不改任何东西
    python scripts/06_reconcile.py --delete-orphans   # 删除孤儿对象（默认只删 1 小时前的，避开正在上传的）
    python scripts/06_reconcile.py --only plans       # 只查某一块：documents / davine / plans
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import alibabacloud_oss_v2 as oss

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # 让脚本能 import tair 包

from tair import config  # noqa: E402
from tair import docs as docs_service  # noqa: E402
from tair import oss as oss_helper  # noqa: E402
from tair import plans as plans_service  # noqa: E402
from tair.console import line, section  # noqa: E402


def list_objects(client: oss.Client, bucket: str, prefix: str) -> dict[str, datetime | None]:
    """列出前缀下全部对象：{key: 最后修改时间}。"""
    found: dict[str, datetime | None] = {}
    token = ""
    while True:
        page = client.list_objects_v2(
            oss.ListObjectsV2Request(
                bucket=bucket, prefix=prefix, max_keys=1000, continuation_token=token or None
            )
        )
        for item in page.contents or []:
            if not item.key.endswith("/"):  # 跳过控制台建的「目录」占位对象
                found[item.key] = item.last_modified
        if not page.is_truncated:
            return found
        token = page.next_continuation_token


def boards(settings: config.Settings) -> dict[str, tuple[str, set[str], bool]]:
    """每块看板：(OSS 前缀, 索引里引用到的 key 集合, 索引是否扫描截断)。"""
    result = {}
    for name, factory in (("documents", docs_service.documents), ("davine", docs_service.davine)):
        store = factory(settings)
        docs, truncated = store.scan()
        keys = {docs_service.key_from_url(d.get("url", "")) for d in docs} - {""}
        result[name] = (store.prefix, keys, truncated)
    plans, truncated = plans_service.scan_plans(settings)
    keys = {plans_service.key_from_url(p.get("oss_url", "")) for p in plans} - {""}
    result["plans"] = (plans_service.plans_prefix(settings), keys, truncated)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawTextHelpFormatter
    )
    parser.add_argument("--delete-orphans", action="store_true", help="删除孤儿对象")
    parser.add_argument("--min-age-hours", type=float, default=1.0, help="只删这么久之前的孤儿")
    parser.add_argument("--only", choices=["documents", "davine", "plans"])
    args = parser.parse_args()

    settings = config.load_settings()
    if not settings.has_docstore:
        print(f"[x] 需要在 .env 里配置：{'、'.join(settings.missing_docstore)}", file=sys.stderr)
        return 2

    client = oss_helper.shared_client(settings)
    cutoff = datetime.now(timezone.utc) - timedelta(hours=args.min_age_hours)
    problems = 0

    for name, (prefix, indexed, truncated) in boards(settings).items():
        if args.only and name != args.only:
            continue
        section(f"{name}：oss://{settings.oss_bucket}/{prefix}")
        if truncated:
            line("[!] 索引超过扫描上限", "结果不完整，跳过这一块，免得误删", indent=2)
            continue
        objects = list_objects(client, settings.oss_bucket, prefix)
        orphans = sorted(set(objects) - indexed)
        dangling = sorted(indexed - set(objects))
        line("OSS 对象数 / 索引行引用数", f"{len(objects)} / {len(indexed)}")
        line("孤儿对象（有对象无索引）", len(orphans))
        for key in orphans:
            line(f"  - {key}", objects[key] or "")
        line("悬空索引（有索引无对象）", len(dangling))
        for key in dangling:
            line(f"  - {key}", "")
        problems += len(orphans) + len(dangling)

        if args.delete_orphans and orphans:
            removed = 0
            for key in orphans:
                modified = objects[key]
                if modified and modified.tzinfo is None:
                    modified = modified.replace(tzinfo=timezone.utc)
                if modified and modified > cutoff:
                    line(f"  跳过（{args.min_age_hours:g} 小时内上传）", key)
                    continue
                client.delete_object(oss.DeleteObjectRequest(bucket=settings.oss_bucket, key=key))
                removed += 1
            line("已删除孤儿对象", removed)

    print()
    print("对账完成：一致。" if problems == 0 else f"对账完成：发现 {problems} 处不一致。")
    return 0 if problems == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
