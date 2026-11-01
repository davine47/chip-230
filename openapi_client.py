"""控制面：用阿里云官方 Python SDK 调 Tair（兼容 Redis）的 OpenAPI。

对应文档《Python SDK 集成示例》：
    pip install alibabacloud_credentials alibabacloud_r_kvstore20150101 alibabacloud_tea_console
    export ALIBABA_CLOUD_ACCESS_KEY_ID / ALIBABA_CLOUD_ACCESS_KEY_SECRET

调用套路固定四步：
    Config(账号/地域)  ->  Client(Config)  ->  XxxRequest(业务参数)
    ->  client.xxx_with_options(request, RuntimeOptions())  ->  resp.body
"""

from __future__ import annotations

from typing import Any

from alibabacloud_r_kvstore20150101 import models as kv_models
from alibabacloud_r_kvstore20150101.client import Client
from alibabacloud_tea_openapi import models as open_api_models
from alibabacloud_tea_util import models as util_models

from config import Settings

# 常见错误码 -> 人话解释（排查时先看 code）
ERROR_TIPS = {
    "InvalidAccessKeyId.NotFound": "AccessKey ID 不存在：检查是否复制完整、末尾有没有多余空格。",
    "InvalidAccessKeyId.Inactive": "AccessKey ID 已被禁用：去 RAM 控制台启用或重新创建。",
    "SignatureDoesNotMatch": "AccessKey Secret 不对（ID 对了但 Secret 错了，或者两台机器时间差太大）。",
    "Forbidden.RAM": "RAM 用户没有这个接口的权限：确认已附加 AliyunKvstoreReadOnlyAccess。",
    "Forbidden": "权限不足：检查 RAM 策略和资源范围。",
    "NoPermission": "权限不足：检查 RAM 策略。",
    "InvalidRegionId.NotFound": "地域 ID 写错了，例如 cn-hangzhou / cn-beijing / cn-shanghai。",
    "Throttling": "被限流了：降低调用频率，或加缓存。",
    "InvalidInstanceId.NotFound": "实例 ID 不存在：确认地域选对了。",
}


def build_client(settings: Settings) -> Client:
    """构造 SDK 客户端。AccessKey 从配置来，地域决定默认 endpoint。"""
    config = open_api_models.Config(
        access_key_id=settings.access_key_id,
        access_key_secret=settings.access_key_secret,
        region_id=settings.region_id,
        connect_timeout=5000,  # 毫秒
        read_timeout=15000,  # 毫秒
    )
    # 一般不用填 endpoint：SDK 会按 RegionId 解析（cn-hangzhou -> r-kvstore.aliyuncs.com）。
    # 只有走特殊网络/代理时才需要手动覆盖。
    if settings.endpoint:
        config.endpoint = settings.endpoint
    return Client(config)


def describe_instances_overview(settings: Settings, instance_ids: str = "") -> dict[str, Any]:
    """文档里的示例接口：查询实例列表及其基本信息。

    返回结构就是文档输出里那个 body（PascalCase 字段）：
        {"Instances": [...], "RequestId": "...", "TotalCount": 2}
    """
    client = build_client(settings)
    request = kv_models.DescribeInstancesOverviewRequest(region_id=settings.region_id)
    if instance_ids:
        request.instance_ids = instance_ids

    response = client.describe_instances_overview_with_options(
        request, util_models.RuntimeOptions()
    )
    return response.body.to_map()


def describe_instance_attribute(settings: Settings, instance_id: str) -> dict[str, Any]:
    """查询单个实例的详细信息（连接地址、端口、状态、白名单等）。

    注意返回结构和 DescribeInstancesOverview 不一样：这里的 Instances 不是数组，
    而是「单个对象包一层」，真正的字段在 DBInstanceAttribute 里。
    """
    client = build_client(settings)
    request = kv_models.DescribeInstanceAttributeRequest(instance_id=instance_id)
    response = client.describe_instance_attribute_with_options(
        request, util_models.RuntimeOptions()
    )
    wrapper = response.body.instances
    # 形态是 Instances -> DBInstanceAttribute -> [ {...} ]，多包了两层
    items = getattr(wrapper, "dbinstance_attribute", None)
    if items is None and isinstance(wrapper, list):
        items = wrapper
    if not items:
        raise RuntimeError(f"接口没有返回实例 {instance_id} 的详细信息")
    return items[0].to_map()


def explain_error(exc: Exception) -> tuple[str, str, str]:
    """把 SDK 异常翻译成 (错误码, 原始信息, 排查建议)。"""
    code = getattr(exc, "code", "") or ""
    message = getattr(exc, "message", "") or str(exc)
    tip = ERROR_TIPS.get(code, "")
    if not tip:
        lowered = str(code).lower()
        for known, known_tip in ERROR_TIPS.items():
            if known.lower() == lowered:
                tip = known_tip
                break
    return code, message, tip
