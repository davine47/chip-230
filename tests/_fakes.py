"""测试用的假 OTS / OSS 客户端：只用得到看板实际调用的那几个方法。

放在单独文件里，test_p2 / test_link 共用，避免两边各写一份、改一处漏一处。
"""

from __future__ import annotations

import types

from _stubs import install_stubs

# 必须先装桩，再导入 tair：tair 里会 import 阿里云 SDK，
# 一旦走上真包，后面再装桩就盖不住了（setdefault 不会替换已导入的模块）
install_stubs()

from tair import oss as oss_helper  # noqa: E402
from tair import tablestore as ts_helper  # noqa: E402
from tair.config import load_settings  # noqa: E402


def settings(**over):
    """把 .env 里的配置换成本地假值（测试不连云，也不需要真凭据）。"""
    base = {"oss_bucket": "b", "ts_instance": "i", "access_key_id": "a", "access_key_secret": "s"}
    s = load_settings()
    return s.__class__(**{**s.__dict__, **base, **over})


class FakeOTS:
    """只实现 put_row / get_row / get_range / delete_row，行数据用 2 元组存，读的时候补时间戳。"""

    def __init__(self):
        self.rows = []
        self.fail_put = False

    def put_row(self, table, row, condition, return_type=None):
        if self.fail_put:
            raise RuntimeError("OTS 写入失败")
        self.rows.append(row)
        return None, None

    def get_row(self, table, primary_key, columns_to_get=None):
        want = dict(primary_key).get("id")
        for row in self.rows:
            if dict(row.primary_key).get("id") == want:
                attrs = [(name, value, None) for name, value in (row.attribute_columns or [])]
                return (
                    None,
                    types.SimpleNamespace(primary_key=row.primary_key, attribute_columns=attrs),
                    None,
                )
        return None, None, None

    def get_range(self, table, direction, start, end, columns_to_get=None, limit=None):
        """一页返回全部行，只带 columns_to_get 里要的列（与真实 SDK 一致）。"""
        rows = []
        for row in self.rows:
            attrs = [
                (name, value, None)
                for name, value in (row.attribute_columns or [])
                if columns_to_get is None or name in columns_to_get
            ]
            rows.append(types.SimpleNamespace(primary_key=row.primary_key, attribute_columns=attrs))
        return None, None, rows, None

    def delete_row(self, table, primary_key, condition):
        want = dict(primary_key).get("id")
        self.rows = [row for row in self.rows if dict(row.primary_key).get("id") != want]


class _Uploader:
    def __init__(self, oss):
        self.oss = oss

    def upload_file(self, request, filepath):
        self.oss.uploaded.append(request.key)
        return types.SimpleNamespace(etag='"abc"', request_id="req-1", upload_id=None)


class FakeOSS:
    def __init__(self, size=10, missing=False, existing=None):
        self.size, self.missing = size, missing
        self.existing = set(existing or [])
        self.deleted: list[str] = []
        self.uploaded: list[str] = []

    def is_object_exist(self, bucket, key):
        return key in self.existing

    def uploader(self):
        return _Uploader(self)

    def presign(self, req, expires=None, expiration=None):
        self.last_expires = expires
        self.last_expiration = expiration
        # PutObjectRequest 有 content_type，GetObjectRequest 没有
        headers = {"Host": "b.oss"}
        if getattr(req, "content_type", None):
            headers["Content-Type"] = req.content_type
        return types.SimpleNamespace(
            url=f"https://{req.bucket}.oss/{req.key}?sig=1",
            method="PUT",
            signed_headers=headers,
        )

    def head_object(self, req):
        if self.missing:
            raise RuntimeError("StatusCode: 404, ErrorCode: NoSuchKey")
        return types.SimpleNamespace(content_length=self.size, etag='"abc"')

    def delete_object(self, req):
        self.deleted.append(req.key)


def use(ots, oss_client):
    """把假客户端塞进共享槽位，看板代码就会用它们而不是真的连云。"""
    ts_helper._SHARED[:] = [ots]
    oss_helper._SHARED[:] = [oss_client]
