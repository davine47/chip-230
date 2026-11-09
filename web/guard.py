"""看板的安全边界：只服务本机、拒绝跨站写请求。纯函数，不依赖 Flask，方便测试。

为什么需要（服务只监听 127.0.0.1 还不够）：
  - CSRF：你浏览器里打开的任何网页，都能向 http://127.0.0.1:8080 发 multipart 表单 POST。
    这类「简单请求」浏览器不做预检，会直接送达，于是别人的页面能静默往你的 OSS 传文件。
    → 所有写请求必须带自定义头 X-Requested-With: chip-230。跨站请求带自定义头会触发 CORS 预检，
      而本服务不回应预检，浏览器就不会把请求发出来。
  - DNS rebinding：攻击者让自己的域名解析到 127.0.0.1，浏览器就把它当「同源」，能读写全部接口。
    这种请求的 Host 头是攻击者的域名 → 只放行 Host 为本机名的请求。
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from urllib.parse import urlsplit

LOCAL_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
CSRF_HEADER = "X-Requested-With"
CSRF_VALUE = "chip-230"
UNSAFE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})


def allowed_hostnames(env: Mapping[str, str] = os.environ) -> frozenset[str]:
    """允许的主机名（不含端口，所以 ssh -L 转发到任意本地端口都能用）。

    额外放行：WEB_HOST（显式绑定到某个地址时），以及 WEB_ALLOWED_HOSTS（逗号分隔）。
    """
    hosts = set(LOCAL_HOSTS)
    web_host = env.get("WEB_HOST", "").strip().lower()
    if web_host and web_host not in {"0.0.0.0", "::"}:
        hosts.add(web_host)
    extra = env.get("WEB_ALLOWED_HOSTS", "")
    hosts |= {h.strip().lower() for h in extra.split(",") if h.strip()}
    return frozenset(hosts)


def hostname_of(host_header: str) -> str:
    """'localhost:8080' → 'localhost'；'[::1]:8080' → '::1'；解析不了返回空串。"""
    try:
        return (urlsplit(f"//{host_header}").hostname or "").lower()
    except ValueError:
        return ""


def check_request(
    method: str,
    host_header: str | None,
    origin: str | None,
    csrf_value: str | None,
    allowed: frozenset[str],
) -> str | None:
    """返回拒绝原因；返回 None 表示放行。"""
    if hostname_of(host_header or "") not in allowed:
        return (
            "Host 不在允许列表内，拒绝访问（防 DNS rebinding）。经代理访问请设置 WEB_ALLOWED_HOSTS"
        )
    if method.upper() in UNSAFE_METHODS:
        if csrf_value != CSRF_VALUE:
            return "缺少防跨站请求头，拒绝写操作"
        # 纵深防御：浏览器带了 Origin 就必须和 Host 完全一致（"null" 来自沙箱 iframe / file://，同样拒绝）
        if origin is not None and urlsplit(origin).netloc.lower() != (host_header or "").lower():
            return "跨站来源，拒绝写操作"
    return None
