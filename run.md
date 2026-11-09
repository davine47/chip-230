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

## 三、确认是否活着

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

## 四、页面一览

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

## 五、常见问题

| 现象 | 原因 / 处理 |
| --- | --- |
| `Address already in use` | 端口被占：`lsof -i :8080` 找到进程杀掉，或 `WEB_PORT=8090` 换端口 |
| 页面顶部显示「离线」 | Redis 连不上：检查 `.env` 的 `REDIS_HOST` / `REDIS_PASSWORD`；**多半是出口 IP 变了**，见 [docs/ip-whitelist.md](docs/ip-whitelist.md) |
| 文档库/ davine 列表空、报错 | OSS 或 Tablestore 没配：`OSS_BUCKET`、`OTS_INSTANCE` 是否填了；表/桶是否已建 |
| 上传报 `413` | 超过该看板的单文件上限（文档库 25MB / davine 512MB） |
| 上传几百 MB 很慢 | 这是 Flask 开发服务器，单进程处理；生产环境请换 gunicorn / waitress |
| 改了模板/静态文件没生效 | Python 代码改动需要重启；模板已开自动重载，浏览器刷新即可（静态文件记得强刷） |
| `ModuleNotFoundError: flask` | 没用项目的 venv：命令前加 `./.venv/bin/`，或先 `source .venv/bin/activate` |

---

## 六、注意

- 默认只监听 `127.0.0.1`。**不要**把 `WEB_HOST` 改成 `0.0.0.0` 暴露到公网——这个服务能直接改线上实例数据、删 OSS 对象。
- Flask 自带的 `app.run()` 只适合本地/内网调试，不要直接当生产服务器。
