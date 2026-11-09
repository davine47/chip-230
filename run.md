# 启动 Web 看板

看板是 `web/app.py` 里一个 Flask 应用，默认监听 `127.0.0.1:8080`。
下面按「第一次跑」和「日常启动」分开写。

---

## 一、第一次跑（装环境 + 配配置）

```bash
cd chip-230

# 1. 虚拟环境 + 依赖
python3 -m venv .venv
./.venv/bin/pip install -r requirements.txt

# 2. 配置（.env 不进 Git，新机器上必须自己建一份）
cp .env.template .env
#   至少填这两项：
#   ALIBABA_CLOUD_ACCESS_KEY_ID=...
#   ALIBABA_CLOUD_ACCESS_KEY_SECRET=...
#   要用 Redis / 文档库 / davine / Plan 库，再补：
#   REDIS_HOST / REDIS_PASSWORD
#   OSS_BUCKET
#   OTS_INSTANCE
```

> 换机器 / 换了网络后，出口 IP 会变，Redis 连不上。先跑一次：
>
> ```bash
> ./.venv/bin/python scripts/allow_my_ip.py
> ```

---

## 二、日常启动

### 前台跑（推荐，Ctrl-C 就停）

```bash
cd chip-230
./.venv/bin/python web/app.py
```

看到这行就说明起来了：

```
看板已启动： http://127.0.0.1:8080   （Ctrl-C 停止）
Redis: r-xxxx.redis.rds.aliyuncs.com:6379   地域: cn-beijing
```

> 前面可能先打印两行 `Native C++ encoder/decoder not available`，那是 Tablestore SDK
> 找不到可选的原生加速模块，不影响使用，忽略即可。

浏览器打开 **http://127.0.0.1:8080**。

### 换端口

```bash
WEB_PORT=8090 ./.venv/bin/python web/app.py
```

### 后台跑（关掉终端也不停）

```bash
cd chip-230
nohup ./.venv/bin/python web/app.py > /tmp/chip-230-web.log 2>&1 &
```

看日志：

```bash
tail -f /tmp/chip-230-web.log
```

停止：

```bash
pkill -f "web/app.py"
```

---

## 三、连不上 Redis？先加白名单

实例默认只放行 `127.0.0.1`，换机器或出口 IP 变了就会「离线」或超时。一条命令修好：

```bash
# 自动探测本机出口 IP，写进专用分组 dev_auto（覆盖该分组，旧 IP 自动失效；需要 AK 有改白名单权限）
./.venv/bin/python scripts/allow_my_ip.py

# 只看当前白名单，不改
./.venv/bin/python scripts/allow_my_ip.py --show

# 手动指定 IP / 实例（多个探测来源结果不一致时，脚本会拒绝自动写入，让你用 --ip 确认）
./.venv/bin/python scripts/allow_my_ip.py --ip 203.0.113.10 --instance-id r-bp1xxxxxxxx

# 多台机器：每台用自己的分组，互不覆盖
./.venv/bin/python scripts/allow_my_ip.py --group dev_mac
```

> 出口 IP 只通过 HTTPS 探测，且至少两个来源一致才采信。脚本不会覆盖 `default` 等系统分组。
> 旧版脚本追加在 `default` 分组里的 IP 不会被自动清理，确认不需要后到控制台手动删除。

加完刷新页面，顶部状态应该从「离线」变成「在线 xx ms」。
完整排查（地址类型、安全组、CIDR 写法、最小权限策略）见 [docs/ip-whitelist.md](docs/ip-whitelist.md)。

---

## 四、确认是否活着

```bash
curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:8080/
# 期望：200
```

各页面都可以单独探：

```bash
for p in / /redis /redis/instance /redis/commands /documents /davine /plans; do
  printf '%-18s ' "$p"
  curl -s -o /dev/null -m 5 -w "%{http_code}\n" "http://127.0.0.1:8080$p"
done
```

---

## 五、页面一览

| 路径 | 内容 |
| --- | --- |
| `/` | 总览，四张卡片 |
| `/redis` | Redis 键空间（增删改查） |
| `/redis/instance` | 实例信息（OpenAPI，只读） |
| `/redis/commands` | 命令族与限制 |
| `/documents` | 文档库：上传到 OSS `agents/docs/`，上限 25MB |
| `/davine` | davine：上传到 `davine_exchange/`，上限 512MB，带进度条 |
| `/plans` | Plan 库：只收 `.md`，索引进 `plans` 表 |

---

## 六、常见问题

| 现象 | 原因 / 处理 |
| --- | --- |
| `Address already in use` | 端口被占：`lsof -i :8080` 找到进程杀掉，或 `WEB_PORT=8090` 换端口 |
| 页面顶部显示「离线」 | Redis 连不上：先 `./.venv/bin/python scripts/allow_my_ip.py`（多半是出口 IP 变了）；再检查 `.env` 的 `REDIS_HOST` / `REDIS_PASSWORD`，详见 [docs/ip-whitelist.md](docs/ip-whitelist.md) |
| 文档库/ davine 列表空、报错 | OSS 或 Tablestore 没配：`OSS_BUCKET`、`OTS_INSTANCE` 是否填了；表/桶是否已建 |
| 上传报 `413` | 超过该看板的单文件上限（文档库 25MB / davine 512MB） |
| 上传几百 MB 很慢 | 这是 Flask 开发服务器，单进程处理；生产环境请换 gunicorn / waitress |
| 改了模板/静态文件没生效 | Python 代码改动需要重启；模板已开自动重载，浏览器刷新即可（静态文件记得强刷） |
| `ModuleNotFoundError: flask` | 没用项目的 venv：命令前加 `./.venv/bin/`，或先 `source .venv/bin/activate` |

---

## 七、注意

- 默认只监听 `127.0.0.1`。**不要**把 `WEB_HOST` 改成 `0.0.0.0` 暴露到公网——这个服务能直接改线上实例数据、删 OSS 对象。
- 看板只接受 Host 为 `127.0.0.1` / `localhost` / `::1` 的请求（防 DNS rebinding），写接口必须带
  `X-Requested-With: chip-230` 请求头（防跨站请求，页面自带的 JS 已经加上）。
  用 `ssh -L` 转发到任意本地端口都能正常访问；如果经由别的主机名访问（如反向代理），
  启动前设置 `WEB_ALLOWED_HOSTS=名字1,名字2`。
  自己用 curl 调写接口时记得带上这个头：`curl -X DELETE -H 'X-Requested-With: chip-230' ...`
- Flask 自带的 `app.run()` 只适合本地/内网调试，不要直接当生产服务器。

---

## 八、测试与对账

```bash
# 回归测试：不连云、不需要装 SDK
./.venv/bin/python -m unittest discover -s tests -v

# OSS 与 Tablestore 对账：默认只读；--delete-orphans 删除 1 小时前的孤儿对象
./.venv/bin/python scripts/06_reconcile.py
```

审查结论与待办见 `docs/code-review-2026-11-10.md`。
