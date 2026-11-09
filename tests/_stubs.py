"""测试用的 SDK 桩：不连云、不装阿里云 SDK / redis 也能跑测试。

只在 sys.modules 里还没有对应模块时才装（setdefault），所有测试文件共用这一份，
保证不管哪个测试先被导入，桩的内容都一致。
"""

from __future__ import annotations

import sys
import types


def _module(name: str, **attrs) -> types.ModuleType:
    mod = types.ModuleType(name)
    mod.__dict__.update(attrs)
    return sys.modules.setdefault(name, mod)


class _Req:
    def __init__(self, **kw):
        self.__dict__.update(kw)


def install_stubs() -> None:
    # ---- tablestore
    class Row:
        def __init__(self, primary_key, attribute_columns=None):
            self.primary_key = primary_key
            self.attribute_columns = attribute_columns or []

    _module(
        "tablestore",
        INF_MIN="\x00MIN",
        INF_MAX="\xffMAX",
        PK_AUTO_INCR="AUTO",
        Row=Row,
        Condition=lambda *a, **k: None,
        RowExistenceExpectation=types.SimpleNamespace(IGNORE="IGNORE"),
        ReturnType=types.SimpleNamespace(RT_PK="RT_PK"),
        OTSClient=object,
    )

    # ---- alibabacloud_oss_v2
    oss_reqs = {
        n: type(n, (_Req,), {})
        for n in (
            "PutObjectRequest",
            "GetObjectRequest",
            "DeleteObjectRequest",
            "ListBucketsRequest",
            "ListObjectsV2Request",
        )
    }
    _module("alibabacloud_oss_v2", Client=object, **oss_reqs)

    # ---- redis
    class RedisError(Exception):
        pass

    class WatchError(RedisError):
        pass

    class ResponseError(RedisError):
        pass

    _module(
        "redis",
        RedisError=RedisError,
        WatchError=WatchError,
        ResponseError=ResponseError,
        Redis=object,
    )

    # ---- 控制面 SDK（tair/openapi.py 在导入时就要用到）
    kv_models = _module("alibabacloud_r_kvstore20150101.models")
    kv_client = _module("alibabacloud_r_kvstore20150101.client", Client=object)
    _module("alibabacloud_r_kvstore20150101", models=kv_models, client=kv_client)
    tea_models = _module("alibabacloud_tea_openapi.models", Config=_Req)
    _module("alibabacloud_tea_openapi", models=tea_models)
    util_models = _module("alibabacloud_tea_util.models", RuntimeOptions=_Req)
    _module("alibabacloud_tea_util", models=util_models)
