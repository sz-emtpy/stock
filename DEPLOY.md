# ChanPicker 部署与运维文档

覆盖两部分：**如何使用**（本地 / 线上功能操作）与**如何部署到服务器**（生产上线 + 定时扫描 + 备份回滚）。

---

## 一、如何使用

### 1. 本地启动

```bash
./run.sh                       # 一键：建 venv → 装依赖 → 启动服务
# 或手动
.venv/bin/python -m uvicorn backend.app:app --host 127.0.0.1 --port 8787
```

打开 http://127.0.0.1:8787

### 2. 页面功能（三个标签）

| 标签 | 做什么 | 怎么用 |
|---|---|---|
| **市场扫描** | 全市场（沪深300）扫描当前符合的股票 | 点「扫描沪深300成分股」→ 进度条自动推进 → 完成后自动列出「近30日一买候选」和「近30日二买信号」；点任意行「查看结构」跳转个股图 |
| **个股查询** | 查询某支股票是否符合 | 输入代码（如 `sh600519`、`sz000063`）→ 回车或点「分析」→ 顶部给出「符合条件 / 当前不符合」结论 + K线结构图；勾选「强制刷新行情」可忽略缓存重新拉取 |
| **关注圈** | 跟踪入圈股票的状态 | 直接查看；状态机：待二买 → 机会已触发 / 一买失败 / 弱跟踪 |
| **三日买点池** | 最近 3 个交易日内可进行的一买/二买/三买票池 | 切换页签或点「刷新」；三个分池各自列出代码/名称/信号日/信号价/强度，点行内「查看结构」跳个股图 |

「三日」= 最近 3 个**交易日**（取自本地 K 线缓存日期集合），不是自然日；同一次回调可能同时满足二买与三买定义，会同时出现在两个池中。

K线图元素：橙色三角=顶分型，蓝色=底分型，灰线=笔，紫色区间=中枢，黄色图钉=一买，绿色图钉=二买，紫色图钉=三买，涨红跌绿（中国市场惯例）。

### 3. 命令行（不启服务也能用）

```bash
.venv/bin/python -m chanpicker.cli scan data.csv                 # 扫描单个 CSV
.venv/bin/python -m chanpicker.cli watch data.csv --output wl.json  # 更新关注圈 JSON
```

### 4. 接口速查

```bash
curl http://127.0.0.1:8787/api/health
curl "http://127.0.0.1:8787/api/analyze?symbol=sh600519"
curl -X POST "http://127.0.0.1:8787/api/market/scan?scope=hs300"
curl "http://127.0.0.1:8787/api/market/candidates?recent_days=30"
curl "http://127.0.0.1:8787/api/market/pools?days=3"
curl "http://127.0.0.1:8787/api/watchlist"
```

---

## 二、部署到服务器

### 0. 部署架构

```
浏览器 → Nginx(80/443) → uvicorn(127.0.0.1:8787) → SQLite(数据目录)
                                    ↓
                            westock CLI（行情数据源）
cron/systemd timer ──每日18:00──→ scripts/daily_scan.py（自动扫描）
```

数据库与日志放在**项目目录之外**的持久路径，便于升级代码时不丢数据。

### 1. 服务器环境要求

- Linux（Ubuntu 20.04+ / CentOS 7+ 均可），Python **3.9+**（已在 3.9.25 上实测通过；代码不使用 3.10+ 语法，类型标注统一用 `Optional`/`Union`）
- 内存 ≥ 1GB（扫描时峰值约 200MB）
- 磁盘 ≥ 2GB（SQLite 日K缓存约几十 MB，留备份空间）
- 开放端口：80/443（Nginx），8787 **仅本机监听，不对公网开放**

快速确认服务器 Python 版本：`python3 -V`。若存在多个版本，可用指定版本建 venv：

```bash
python3.11 -m venv .venv     # 优先用 ≥3.9 的任意版本；没有 3.11 用系统 3.9 也可以
```

### 2. 安装 westock 数据源并完成授权（关键前置）

行情来自腾讯自选股 CLI（Go 单文件二进制）。**服务器上必须完成一次授权**，否则所有行情接口返回 503。

#### 2.1 安装 CLI

> 安装脚本原本只存在于 `westockdata` 技能包内（技能包默认不在服务器上）。
> **现已随本项目提供一份副本：`scripts/westock-setup.sh`**，服务器 `git pull` 后即可直接用。

**方式 A：用项目内的脚本（推荐）**

```bash
cd /opt/chanpicker && git pull
bash scripts/westock-setup.sh                  # 零参数，自动识别 linux/amd64、linux/arm64
```

**方式 B：不依赖任何脚本，直接下载（已验证可达）**

```bash
VER=v0.0.3
BASE=https://stockbuddy.qq.com/release/wbgeneral/cli/$VER
ARCH=$(uname -m); case "$ARCH" in x86_64|amd64) A=amd64;; aarch64|arm64) A=arm64;; esac
curl -fsSL "$BASE/westock-linux-$A" -o /tmp/westock
# 校验（v0.0.3 官方值）
#   linux-amd64: bfb260c0a8a4b6d89ef97a9e8a396d6b7522cdb6684b7f8833f06854ee9cdf64
#   linux-arm64: 496ec3adc80d426951478ca1e427ac34d63d47e9d867f9b9932039b5b6470a1c
sha256sum /tmp/westock
sudo install -m 755 /tmp/westock /usr/local/bin/westock
```

**方式 C：在能联网的机器上装好后拷二进制过去**

```bash
bash scripts/westock-setup.sh                   # 装到 ~/.local/bin/westock
scp ~/.local/bin/westock user@server:/tmp/westock
ssh user@server 'sudo install -m 755 /tmp/westock /usr/local/bin/westock'
```

> 脚本**只写二进制，不改 PATH / shell 配置**，默认装到 `~/.local/bin`（可用 `WESTOCK_INSTALL_DIR` 覆盖），并强制做 SHA256 校验。
> Windows 服务器请用技能包内的 `setup.ps1`；有 Node ≥ 18 时也可 `node setup.cjs`。

安装后验证（命令找不到时用绝对路径，或把目录加进 PATH）：

```bash
export PATH="$HOME/.local/bin:$PATH"     # 仅当前会话；永久生效需写入 ~/.bashrc / ~/.zshrc
westock --help                            # 能列出子命令即安装成功
```

#### 2.2 完成授权（设备码流程，5 分钟有效）

**第 1 步：发起**

```bash
westock auth status
```

| 输出 | 含义 / 处理 |
|---|---|
| `READY` | 已授权，直接进入第 3 步验证 |
| `AUTH_REQUIRED:<url>` | 把 `<url>` 用**任意一台电脑的浏览器**打开完成授权（设备码授权，不要求浏览器与服务器同机），随后**立刻进入第 2 步** |
| `ERROR:*` | 按第 4 步错误表处理 |

**第 2 步：轮询等待（不要停下发问）**

```bash
for i in $(seq 1 30); do
  out=$(westock auth fetch)
  case "$out" in
    TOKEN_READY)    echo "已授权"; break ;;
    ERROR:expired)  echo "链接已过期，回到第 1 步重新发起"; break ;;
    *)              sleep 10 ;;      # 含 not_authorized：用户可能还在扫码，继续等
  esac
done
```

> ⚠️ 轮询期间**只能执行 `auth fetch`，不要再执行 `auth status`**：`status` 发现无 key 时会重新下发授权码并覆盖本地记录，导致正在扫的码作废（表现为「刚授权完却仍返回 not_authorized」）。

**第 3 步：验证**

```bash
westock quote sh600519      # 能返回贵州茅台行情即成功
```

**第 4 步：异常与兜底**

| 场景 | 处理 |
|---|---|
| `ERROR:not_authorized` | 尚未完成扫码，继续轮询 |
| `ERROR:expired` | 链接超过 5 分钟或已被领取，回第 1 步重新发起 |
| `ERROR:cancelled` | 用户在授权页取消，停止轮询；需继续则回第 1 步 |
| `ERROR:network:*` / `ERROR:http_*` | 检查服务器出网后重试 |
| 自动流程始终失败 | 授权页会显示专属 APIKey，手动写入：`westock auth set "<WZQ_APIKEY>"` |
| 想退出 / 取消授权 | `westock auth clear` |

#### 2.3 凭证位置与 systemd 的配合

- 凭证目录默认是执行用户的 `~/.westock`，可用环境变量 `WESTOCK_HOME` 覆盖。
- **systemd 服务必须与完成授权的是同一个用户**，否则读不到凭证（下文统一用 `chanpicker` 用户）。
- 若二进制装在 `~/.local/bin`，需把该目录写进 unit 的 PATH（见第 5 节）。

### 3. 部署代码

```bash
# 创建专用用户与目录
sudo useradd -r -m -s /bin/bash chanpicker
sudo mkdir -p /opt/chanpicker /var/lib/chanpicker /var/log/chanpicker
sudo chown -R chanpicker:chanpicker /opt/chanpicker /var/lib/chanpicker /var/log/chanpicker

# 方式A：git（推荐）
sudo -u chanpicker git clone <你的仓库地址> /opt/chanpicker

# 方式B：本机打包上传
tar czf chanpicker.tgz --exclude=.venv --exclude=data --exclude=__pycache__ .
scp chanpicker.tgz user@server:/tmp/
sudo -u chanpicker tar xzf /tmp/chanpicker.tgz -C /opt/chanpicker
```

### 4. 安装依赖

```bash
sudo -u chanpicker bash -c 'cd /opt/chanpicker && python3 -m venv .venv && .venv/bin/pip install --upgrade pip && .venv/bin/pip install -e .'
```

### 5. systemd 服务（开机自启 + 崩溃重启）

创建 `/etc/systemd/system/chanpicker.service`：

```ini
[Unit]
Description=ChanPicker 缠论选股服务
After=network.target

[Service]
Type=simple
User=chanpicker
Group=chanpicker
WorkingDirectory=/opt/chanpicker
Environment=CHANPICKER_HOST=127.0.0.1
Environment=CHANPICKER_PORT=8787
Environment=CHANPICKER_DB=/var/lib/chanpicker/chanpicker.db
Environment=CHANPICKER_LOG_LEVEL=info
# 凭证目录必须指向「完成授权的那个用户」的家目录，避免服务读不到 ~/.westock
Environment=HOME=/root
Environment=WESTOCK_HOME=/root/.westock
# PATH 必须包含 westock 所在目录（装在 ~/.local/bin 或 /usr/local/bin 均可）
Environment=PATH=/opt/chanpicker/.venv/bin:/root/.local/bin:/usr/local/bin:/usr/bin:/bin
ExecStart=/opt/chanpicker/.venv/bin/python -m backend.server
Restart=always
RestartSec=5
StandardOutput=append:/var/log/chanpicker/service.log
StandardError=append:/var/log/chanpicker/service.log

[Install]
WantedBy=multi-user.target
```

启动并设为开机自启：

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now chanpicker
sudo systemctl status chanpicker
curl http://127.0.0.1:8787/api/health      # 期望 westock_authorized: true
```

> ⚠️ `CHANPICKER_DB` 指向 `/var/lib/chanpicker/`（项目外），避免升级代码时误删数据。
>
> ⚠️ `HOME` / `WESTOCK_HOME` 必须与**完成授权时用的用户**一致。上面示例是 root 部署；若改用 `User=chanpicker`，两处要一起改成 `/home/chanpicker` 与 `/home/chanpicker/.westock`，且必须 `sudo -u chanpicker -H westock auth status` 重新授权。

**以 root 部署时的授权（一步一步）**

```bash
# 1) 确认当前是 root，且 CLI 用的就是它
id -un                       # 期望 root
command -v westock

# 2) 授权状态
westock auth status          # READY=已授权；AUTH_REQUIRED:<url>=需授权

# 3) 若需授权：浏览器打开 <url>（任意电脑，5 分钟内），然后轮询
for i in $(seq 1 30); do
  out=$(westock auth fetch)
  case "$out" in
    TOKEN_READY)   echo 已授权; break ;;
    ERROR:expired) echo 过期; break ;;
    *)             echo "$out"; sleep 10 ;;
  esac
done

# 4) 确认凭证落在 /root/.westock
ls -d /root/.westock && westock quote sh600519 | head -3

# 5) 让服务也认这个目录：临时验证
WESTOCK_HOME=/root/.westock HOME=/root /opt/chanpicker/.venv/bin/python -m backend.server
# 另开一个终端
curl -s http://127.0.0.1:8787/api/health | grep -E 'westock_authorized|westock_home|service_user'
```

确认无误后，把 `HOME` / `WESTOCK_HOME` 写进 systemd unit（见上），`systemctl daemon-reload && systemctl restart chanpicker`。

### 6. Nginx 反向代理（对外 80/443）

```nginx
server {
    listen 80;
    server_name your.domain.com;

    location / {
        proxy_pass http://127.0.0.1:8787;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_read_timeout 300s;      # 全市场扫描耗时较长
    }

    location /static/ {
        alias /opt/chanpicker/frontend/;
        expires 7d;
    }
}
```

```bash
sudo nginx -t && sudo systemctl reload nginx
# HTTPS（推荐）
sudo certbot --nginx -d your.domain.com
```

### 7. 每日自动扫描（定时跟踪）

系统提供 `scripts/daily_scan.py`：启动一次全市场扫描并等待完成，适合 cron。

```bash
# crontab -u chanpicker -e
0 18 * * 1-5 cd /opt/chanpicker && CHANPICKER_DB=/var/lib/chanpicker/chanpicker.db /opt/chanpicker/.venv/bin/python scripts/daily_scan.py >> /var/log/chanpicker/scan.log 2>&1
```

说明：
- 交易日 18:00 触发（盘后，当日 K 线已定稿）
- 脚本最长等待 1 小时；超时退出但后台任务继续，下次扫描会基于缓存增量补齐
- 扫描结果写入 SQLite，页面「市场扫描」标签自动展示最新一批候选

> 也可用 systemd timer 替代 cron，二选一即可。

### 8. 数据备份

```bash
# 每日 20:00 备份 SQLite（复制式备份，安全且可热备）
0 20 * * * sqlite3 /var/lib/chanpicker/chanpicker.db ".backup /var/lib/chanpicker/backup/chanpicker-$(date +\%F).db"
# 保留 14 天
0 21 * * * find /var/lib/chanpicker/backup -name 'chanpicker-*.db' -mtime +14 -delete
```

恢复：`systemctl stop chanpicker` → 用备份文件覆盖库文件 → `systemctl start chanpicker`。

### 9. 日志与监控

| 对象 | 位置/命令 |
|---|---|
| 服务日志 | `/var/log/chanpicker/service.log`、`journalctl -u chanpicker -f` |
| 扫描日志 | `/var/log/chanpicker/scan.log` |
| 健康检查 | `curl http://127.0.0.1:8787/api/health`（关注 `westock_authorized`） |
| 扫描状态 | `curl http://127.0.0.1:8787/api/market/scan/status` |

建议监控项：健康接口 5xx、扫描任务失败数、磁盘剩余空间、备份文件是否当日生成。

### 10. 升级与回滚

```bash
# 升级
sudo -u chanpicker bash -c 'cd /opt/chanpicker && git pull && .venv/bin/pip install -e .'
sudo systemctl restart chanpicker
curl http://127.0.0.1:8787/api/health

# 回滚
sudo -u chanpicker git -C /opt/chanpicker checkout <上一个 commit>
sudo systemctl restart chanpicker
```

数据目录在 `/var/lib/chanpicker/`，不受代码回滚影响。

### 11. 安全建议

- 服务只监听 `127.0.0.1`，对外只暴露 Nginx；不要直接把 8787 暴露公网
- 用 HTTPS；如需内网访问控制，在 Nginx 加 `allow/deny` 或基础认证
- 以非 root 用户（`chanpicker`）运行；数据库与日志目录权限收紧
- westock 凭证存在 `~/.westock`，不要提交进代码仓库

### 12. 上线前自检清单（部署完必跑）

```bash
# 1) 健康检查（关键看 westock_authorized）
curl -s http://127.0.0.1:8787/api/health
# 2) 页面与静态资源
curl -s -o /dev/null -w "index=%{http_code}\n" http://127.0.0.1:8787/
curl -s -o /dev/null -w "echarts=%{http_code}\n" http://127.0.0.1:8787/static/vendor/echarts.min.js
# 3) 单只个股端到端（应返回 qualifies 字段与 bars）
curl -s "http://127.0.0.1:8787/api/analyze?symbol=sh600519" | head -c 300
# 4) 候选与关注圈
curl -s "http://127.0.0.1:8787/api/market/candidates?recent_days=30" | head -c 300
curl -s http://127.0.0.1:8787/api/watchlist | head -c 300
# 5) 首次全市场扫描（无缓存约 2–5 分钟；有当日缓存秒级返回）
curl -X POST "http://127.0.0.1:8787/api/market/scan?scope=hs300"
curl -s http://127.0.0.1:8787/api/market/scan/status | head -c 200
```

浏览器侧自检（可选，有 Chrome/Chromium 时）：无头打开页面并断言「候选行数 > 0、个股查询出现结论横幅与 K 线画布、控制台零报错」。本机已按此方式实测通过（Chrome 155 无头 + CDP：首页 43 条一买候选 / 4 条二买信号，个股查询返回结论且画布 1318×520，关注圈 268 行，控制台与页面错误均为 0）。

### 13. 常见问题

| 现象 | 原因 | 处理 |
|---|---|---|
| 安装时报 `Package 'chanpicker' requires a different Python: 3.9.x not in '>=3.11'` | 旧版本项目把 `requires-python` 写成 `>=3.11`，而服务器是 3.9 | 拉取最新代码（已放宽为 `>=3.9`）后重装；或改用 `python3.11 -m venv .venv` |
| `.venv` 已用旧版代码建过、装不上 | venv 元数据缓存 | `rm -rf /opt/chanpicker/.venv` 后重跑安装命令 |
| `bash: westock: command not found` | 安装脚本不改 PATH | 用绝对路径 `~/.local/bin/westock`，或 `export PATH="$HOME/.local/bin:$PATH"`；systemd 则在 unit 的 `Environment=PATH` 里加该目录 |
| `/api/health` 显示 `westock_authorized: false` | 未授权，或 systemd 运行用户与授权用户不同 | 用**服务同用户**执行 `westock auth status` → 浏览器打开 `AUTH_REQUIRED:` 后的链接 → `westock auth fetch` 轮询到 `TOKEN_READY` |
| **命令行 `westock quote` 正常，但服务仍报未授权** | 服务进程与授权的不是同一个用户，凭证目录不同 | 见下方「用户不一致排查」 |

#### 用户不一致排查（最常见）

health 接口会额外返回三个定位字段：`westock_bin`（实际用到的二进制）、`westock_home`（凭证目录）、`service_user`（服务运行用户）。

```bash
curl -s http://127.0.0.1:8787/api/health
# 对比：命令行这边
echo "shell: user=$(id -un) home=$HOME"
command -v westock

# 服务进程用的是谁
ps -o user=,pid=,cmd= -C uvicorn 2>/dev/null || ps -ef | grep -E 'backend.server|uvicorn' | grep -v grep

# 用「服务同用户」重新授权（假设服务用户是 chanpicker）
sudo -u chanpicker -H westock auth status
sudo -u chanpicker -H westock auth fetch     # 轮询到 TOKEN_READY
```

判定：`service_user` 与你授权时用的用户不一致 → 必须换成 `service_user` 那个用户重新授权；一致但仍失败 → 看 `westock_detail` 里的原始报错。
| 明明扫码完成，仍返回 `not_authorized` | 轮询期间又执行了 `auth status`，重新下发了授权码 | 回第 2.2 节第 1 步重新发起，之后只用 `auth fetch` 轮询 |
| 分析接口 503「未授权」 | 同上 | 同上 |
| 扫描报「N 只获取失败」 | 数据源限流 | 已内置批间节流 + 退避重试；失败股票会在下次扫描基于缓存增量补齐，多跑一轮通常收敛为 0 |
| 分析接口 503「未授权」 | 同上 | 同上 |
| 扫描报「N 只获取失败」 | 数据源限流 | 已内置批间节流 + 退避重试；失败股票会在下次扫描基于缓存增量补齐，多跑一轮通常收敛为 0 |
| 端口占用 | 8787 已被占用 | 改 `CHANPICKER_PORT` 并同步 Nginx `proxy_pass` |
| 页面图表空白 | ECharts 未加载 | 已内置本地 `frontend/vendor/echarts.min.js`；若缺失，检查 `/static/vendor/` 是否可访问（Nginx 已单独配置 `/static/`） |

---

## 免责声明

本系统为技术分析研究工具，所有信号基于公开行情数据与缠论量化规则推导，仅供参考，不构成投资建议。市场有风险，投资需谨慎。过往表现不预示未来收益。
