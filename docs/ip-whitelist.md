# 把本机 IP 加进白名单（换机器 / IP 变了看这份）

> 适用场景：新机器连不上实例、报 `Timeout connecting to server`、看板一直转圈、`redis-cli` 卡住。
> 九成是**出口 IP 不在白名单**里。这份文档可以脱离仓库单独照着做。

---

## 换一台新机器？照这 5 步走

```bash
# 1. 装依赖
python3 -m venv .venv && ./.venv/bin/pip install -r requirements.txt

# 2. 配好身份和连接信息（新机器上 .env 不会自动跟过来）
cp .env.template .env
#   填 ALIBABA_CLOUD_ACCESS_KEY_ID / SECRET、REDIS_HOST、REDIS_PASSWORD
#   或者直接从旧机器把 .env 拷过来，内容就是这几项

# 3. 看新机器的出口 IP
curl -4 -s http://ipv4.icanhazip.com

# 4. 加白名单（脚本会自动探测 IP）
python scripts/allow_my_ip.py

# 5. 验证
python scripts/00_doctor.py      # 四项全绿就通了
```

只有第 3、4 步是本文的重点，其余是常规准备。**换机器最容易忘的是第 2 步的 `.env`**——
它被 `.gitignore` 排除了，`git clone` 下来是没有的。

---

## 0. 先搞清楚三件事

连一台阿里云 Tair（兼容 Redis）实例，网络这条路要过三关，缺一不可：

| 关卡 | 说明 | 怎么查 |
| --- | --- | --- |
| ① 地址类型 | 控制台的「内网地址」只有**同 VPC 的 ECS** 能连；本机/公网机器必须用**公网连接地址** | 控制台「连接信息」，或 `DescribeDBInstanceNetInfo` |
| ② 白名单 | 实例默认只放行 `127.0.0.1`，等于谁都不让连 | 控制台「白名单设置」，或 `DescribeSecurityIps` |
| ③ 安全组 / ACL | 走 ECS 时还要看安全组是否放行 6379 | ECS 控制台 |

本文只解决 ②。如果连 ① 都没过（比如实例还没申请公网地址），先看文末的「没有公网地址怎么办」。

---

## 1. 取本机当前的出口 IP

```bash
curl -4 -s --max-time 6 http://ipv4.icanhazip.com
# 例：203.0.113.10
```

几个可替换的源（任意一个通了就行）：

```bash
curl -4 -s https://api.ipify.org
curl -4 -s https://ifconfig.me/ip
```

> **坑**：如果这台机器装了代理或分流软件，浏览器/命令行看到的出口 IP 可能不一样。
> 实测遇到过 `curl` 拿到 A、Python `urllib` 拿到 B 的情况，最后以**能连通 Redis 的那个**为准。
> 拿不准就两个都加进白名单，验证连通后再删掉多余的那个。

---

## 2. 加白名单：三种方式

### 方式 A：控制台点（最直观）

1. 打开 [Tair 实例列表](https://kvstore.console.aliyun.com/)，选对**地域**
2. 点实例 ID 进详情 → 左侧「**白名单设置**」
3. 在 `default` 分组里，把 IP 追加到输入框（多个用英文逗号分隔）
   - 例：`203.0.113.10,127.0.0.1`
4. 「确定」保存，**不需要重启实例**，一般几秒内生效

### 方式 B：脚本一键（本仓库自带，推荐）

需要 AccessKey 具备改白名单的权限（`AliyunKvstoreFullAccess` 或自定义策略，见第 6 节）。

```bash
# 在仓库根目录
python scripts/allow_my_ip.py            # 自动探测出口 IP 并追加
python scripts/allow_my_ip.py --show     # 只看当前白名单，不改
python scripts/allow_my_ip.py --ip 203.0.113.10   # 手动指定 IP
python scripts/allow_my_ip.py --instance-id r-bp1xxxxxxxx
```

输出示例：

```
实例: r-bp1xxxxxxxxxxxx   地域: cn-hangzhou
本机出口 IP: 203.0.113.10
[✓] 已追加 203.0.113.10
当前白名单:
  default            203.0.113.10,127.0.0.1
  hdm_security_ips   100.104.168.0/24,100.104.224.0/24
```

### 方式 C：一句 OpenAPI（贴到别的脚本 / CI 里）

> 下面这段依赖仓库里的 `tair/` 包，请在**仓库根目录**下运行，或把仓库根目录加进 `PYTHONPATH`。

```python
from alibabacloud_r_kvstore20150101 import models as m
from alibabacloud_tea_util import models as util_models
from tair import config
from tair.openapi import build_client

client = build_client(config.load_settings())
client.modify_security_ips_with_options(
    m.ModifySecurityIpsRequest(
        instance_id="r-bp1xxxxxxxxxxxx",
        security_ips="203.0.113.10",
        modify_mode="Append",      # Append 追加 / Delete 删除 / Cover 覆盖
    ),
    util_models.RuntimeOptions(),
)
```

`security_ip_group_name` 不填就是默认组 `default`；要多组隔离时再指定。

---

## 3. 验证连通

```bash
# 最快：TCP 通不通（3 秒超时）
python - <<'PY'
import socket
s = socket.socket(); s.settimeout(3)
try:
    s.connect(("r-bp1xxxxxxxx.redis.rds.aliyuncs.com", 6379))
    print("TCP 通")
except Exception as e:
    print("TCP 不通:", type(e).__name__)
PY

# 再验证协议层
redis-cli -h r-bp1xxxxxxxx.redis.rds.aliyuncs.com -p 6379 -a '<密码>' --no-auth-warning ping
# 期望：PONG

# 本仓库也可以直接跑
python scripts/00_doctor.py
python scripts/03_end2end.py
```

---

## 4. 规则怎么写

| 写法 | 含义 | 例子 |
| --- | --- | --- |
| 单个 IP | 只要这一台 | `203.0.113.10` |
| CIDR 网段 | 整个办公网/机房出口段 | `203.0.113.0/24` |
| 多组 | 按环境拆（生产/测试） | 新建 `prod`、`dev` 两个组，各自填 IP |
| 默认组 | 不改组名就是 `default` | — |

- 多个 IP 用**英文逗号**分隔，不要有空格
- `0.0.0.0/0` 等于全网放行，**不要用**
- 家里/公司出口 IP 会变，建议同时保留两个常用网络，或者干脆每次变化时跑一次脚本

---

## 5. 排查表

| 现象 | 大概率原因 | 处理 |
| --- | --- | --- |
| 一直卡住几十秒，最后 `Timeout connecting to server` | IP 不在白名单（SYN 被丢） | 本文第 1~2 步 |
| 立刻 `Connection refused` | 端口不对 / 连的是内网地址但不在 VPC | 换公网地址，或从同 VPC 的 ECS 连 |
| `NOAUTH` / `WRONGPASS` | 密码或自定义账号用户名不对 | 检查 `REDIS_PASSWORD` / `REDIS_USERNAME` |
| `Connection reset by peer` | 用了 TLS 端口但没开 SSL，或反过来 | `REDIS_SSL=true` |
| 昨天好好的，今天连不上 | **出口 IP 变了**（最常见） | 重跑 `python scripts/allow_my_ip.py` |
| 白名单改了还是不行 | 加到了另一个地域 / 另一台实例；或有多个安全组 | 核对实例 ID 与地域 |
| 从 ECS 连不上内网地址 | 不在同一 VPC / 不同交换机 ACL | 换到同 VPC，或用公网地址 |

查看当前白名单（只读，不改）：

```bash
python scripts/allow_my_ip.py --show
```

---

## 6. 最小权限策略（给运维/同事用）

不建议把主账号 AK 给出去。在 RAM 里建一个只改白名单的自定义策略：

```json
{
  "Version": "1",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": [
        "kvstore:DescribeSecurityIps",
        "kvstore:ModifySecurityIps",
        "kvstore:DescribeInstancesOverview"
      ],
      "Resource": "*"
    }
  ]
}
```

附加给 `scripts/allow_my_ip.py` 使用的那个 RAM 用户即可，比 `AliyunKvstoreFullAccess` 小得多。

---

## 7. 安全提醒

- 只放行**确实需要**的 IP，用完就删：`modify_mode="Delete"`
- 定期清掉历史 IP（换网络、离职同事的机器）
- 白名单只是第一层，实例密码照样要强；公网实例建议同时开 TLS
- 不要把 `0.0.0.0/0` 写进白名单，也不要把它写进任何脚本默认值

---

## 8. 附：没有公网地址怎么办

实例只有内网地址时（`DescribeDBInstanceNetInfo` 只返回 `IPType=Private`），本机是连不上的：

```python
# 申请一个公网连接地址（写操作，会产生公网流量费用）
from alibabacloud_r_kvstore20150101 import models as m
from alibabacloud_tea_util import models as util_models
from tair import config
from tair.openapi import build_client

client = build_client(config.load_settings())
client.allocate_instance_public_connection_with_options(
    m.AllocateInstancePublicConnectionRequest(
        instance_id="r-bp1xxxxxxxxxxxx",
        connection_string_prefix="my-public",   # 8~64 位，小写字母/数字/连字符
        port="6379",
    ),
    util_models.RuntimeOptions(),
)
```

申请完再 `DescribeDBInstanceNetInfo` 就能看到 `IPType=Public` 的地址，然后回到本文第 1 步加白名单。

不想开公网的话，把脚本/服务部署到**同 VPC 的 ECS** 上，直接用内网地址连，也不需要加公网 IP 白名单（但内网 IP 仍要在白名单里）。
