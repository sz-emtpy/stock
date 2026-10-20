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

### 2. 页面功能（四个标签）

| 标签 | 做什么 | 怎么用 |
|---|---|---|
| **市场扫描** | 全市场扫描当前符合的股票 | 点「扫描全部A股」→ 进度条自动推进（含预计剩余时间）→ 完成后自动列出「近30日一买候选」和「近30日二买信号」；旁边「仅沪深300（快速）」用于秒级验证。所有表格**点表头即可排序**，默认按二买日 / 信号日降序，单表超过 300 行时折叠并给「显示全部」。扫描卡片下方是**历史买点成功率**面板（一买/二买成功率 + 关注圈进出圈计数）。列表数据来自 `signals` 表（**不是**扫描任务的快照），所以窗口与「三日买卖点池」同源可对照 |
| **个股查询** | 查询某支股票是否符合 + 看买卖点位置 | 输入代码（如 `sh600519`、`sz000063`）→ 回车或点「分析」→ 顶部给出「符合条件 / 当前不符合」结论 + K线结构图。右上角可切换 **日线 / 30分钟 / 15分钟**；勾选「强制刷新行情」可忽略缓存重新拉取。K线图上叠加**背驰段**（灰虚线=前段力度，彩线=背驰段，端点标注力度值），默认只描最近一个，勾选图例旁的「显示全部背驰段」可全出。从列表点「查看结构」进入后可用 **← / →**（或工具栏按钮）切换上一只/下一只，`Esc` 返回来源列表 |
| **关注圈** | 跟踪入圈股票的状态 | 直接查看；**一买失败与二买失败即剔除出圈**，在圈只剩待二买 / 弱跟踪 / 机会已触发。工具栏显示「在圈 N 只 · 已出圈 M 只」，点**「重评关注圈」**用本地 K 线缓存重跑一遍（不联网，全量约 1~2 秒）——二买失败只能在后续 K 线上观测到，新增交易日后必须点一次才会翻状态 |
| **三日买卖点池** | 最近 3 个交易日内的买点池 **+ 卖点风险池** | 切换页签或点「刷新」；买点三个分池（一买/二买/三买）+ 卖点两个分池（一卖风险/二卖风险），各自列出代码/名称/信号日/信号价/强度，点行内「查看结构」跳个股图 |

「三日」= 最近 3 个**交易日**（取自本地 K 线缓存日期集合），不是自然日；同一次回调可能同时满足二买与三买定义，会同时出现在两个池中。

> **胜算列 = 体系分（命中特征增益之和）/ 该类买点历史胜率**。
> 体系分来自 `signal_outcomes` 的特征快照，与「体系选股」页同一个打分引擎、同一个增益快照，
> 所以同一个买点在池里和选股页看到的分数一定相同。体系分 ≥ 0 表示这段结构历史上比基准更容易拉升，
> 负值表示更差；「—」表示该买点还没进样本库（`rebuild` 之后新扫出来的），不伪造 0 分。

#### 2.0.1 四个视图的数量关系（**同一买点数为什么不同**）

四个视图不是同一口径，混在一起直接相减必然对不上。自 2026-10 起口径固定为：

| 视图 | 数据源 | 窗口 | 去重 | 过滤 |
|---|---|---|---|---|
| 市场扫描 | `signals` 表 | 最近 **30 交易日**（可调 `recent_days`） | 同股票同类型保留最新一条 | 无 |
| 三日买卖点池 | `signals` 表 | 最近 **3 交易日** | 同上 | 无 |
| 体系选股 | `signal_outcomes` 表 | 最近 3 交易日 | 同上 | 体系分 ≥ `min_score`（默认 0） |
| 关注圈 | `watchlist` 表（状态机） | 全历史累积 | 一只股票一条 | 只返回跟踪中状态 |

因此：

- **市场扫描传 `recent_days=3` 时，与三日买卖点池的数字和条目完全一致**（同一条 SQL、同一套去重），只是窗口更长时数量更多；
- **体系选股 = 三日池中体系分达标的子集**，因为多了 `min_score` 过滤，数量只会 ≤ 池子；
- **关注圈天然小于池子**：一买失败（`FAILED`）、二买失败（`SECOND_BUY_FAILED`）、二买守住（`SECOND_BUY_OK`）三种终态都会出圈，且一只股票只保留当前一轮记录。

`scan_hits` 表仍保留，但只服务「扫描历史」页签——它的主键是
`(run_id, symbol, kind, trade_date)`，能存下一次扫描里同一股票的全部历史买点。
2026-10 之前的库主键缺 `trade_date`，会让每股票每类只剩一条（实测同一 20 交易日窗口
`signals` 一买 1404 条 vs `scan_hits` 145 条），服务启动时会自动迁移，无需手工处理。

#### 2.0.2 历史买点成功率口径

| 指标 | 分子 | 分母 |
|---|---|---|
| 一买成功率 | 一买入圈后最终引出二买的记录（机会已触发 + 二买守住 + 二买失败） | 已终结的一买记录（上述三类 + 一买失败 + 弱跟踪）；仍在「待二买」的单列进行中、不计入 |
| 二买成功率 | 二买确认后 `max_wait_bars`（默认 30 根）考验窗口内守住二买低点的记录（二买守住 + 仍在考验期的机会已触发） | 已确认过二买的记录（机会已触发 + 二买守住 + 二买失败） |

窗口之外的破位**不计**二买失败——那可能是后续大盘整体回落，记在这笔二买头上会把成功率做虚。
反过来，考验期满且期间一直守住 → 归档为 **`SECOND_BUY_OK`（二买守住）**并出圈，
不会永远停在「机会已触发」占着跟踪位（否则 `first_buy_date` 会停留在几百天前，
同一只股票后来的新一买也无法开启新一轮跟踪）。
全部统计自本地关注圈历史记录，样本随时间累积才会稳定。

### 2.1 扫描范围与耗时（重要）

| 项 | 说明 |
|---|---|
| 证券池来源 | `westock universe --market hs --type stock`，数据源声明总数（当前 5576 只）会与解析行数比对，不一致直接报错，避免被返回上限静默截断 |
| 剔除口径 | 剔除**北交所**（bj 前缀）与**风险警示股**（ST / *ST / SST / S*ST）。当前 5576 只 → 剔除北交所 350 只 + ST 198 只（另 3 只既是北交所又是 ST，计入北交所）→ **实际扫描 5028 只** |
| 首次全量耗时 | 实测 300 只冷启动 82 秒（每批 20 只 + 2 秒间隔），线性外推 5028 只约 **26 分钟**；叠加数据源限流退避（20s/40s），现实 **30–50 分钟** |
| 后续扫描 | 已取过「最新交易日」数据的股票直接复用缓存，秒级完成 |
| 磁盘占用 | 单根 K 线约 248 字节，全量约 220 万根 → 数据库约 **450–550 MB**（加索引后）。部署前确认磁盘余量 ≥ 3 GB（含备份） |
| 断点续跑 | 扫描中断时 run 状态记为 `interrupted`，重新扫描会跳过已入库的股票继续；如需彻底重取，加 `force=true` |
| 定时任务 | `scripts/daily_scan.py` 默认 `all`，等待窗口已放宽到 3 小时；`--scope hs300` 可切回快速模式 |

K线图元素：橙色三角=顶分型，蓝色=底分型，灰线=笔，紫色区间=中枢，黄色图钉=一买，绿色图钉=二买，紫色图钉=三买；**浅红向下箭头=一卖（顶背驰），深红向下箭头=二卖（反弹不创新高）**，箭头标在关键高点上。**灰虚线=背驰前段，彩色实线=背驰段**（颜色随信号类型：一买黄/二买绿/三买紫/一卖浅红/二卖深红），端点标注该段 MACD 柱面积（力度）。涨红跌绿（中国市场惯例）。

> **买点与卖点的关系**：买点用于选股（决定「是否符合买入条件」）；卖点只是**持仓风险提示**，不参与选股判定。「个股查询」里，若该级别最新信号是卖点，会在结论上方额外显示一条红色风险提示。"  

### 3. 命令行（不启服务也能用）

```bash
.venv/bin/python -m chanpicker.cli scan data.csv                    # 扫描单个 CSV（日线）
.venv/bin/python -m chanpicker.cli scan m15.csv --period m15        # 分钟级 CSV 需显式标周期
.venv/bin/python -m chanpicker.cli watch data.csv --output wl.json  # 更新关注圈 JSON
```

### 4. 接口速查

```bash
curl http://127.0.0.1:8787/api/health                     # 含 periods / cached（各周期缓存量）
curl "http://127.0.0.1:8787/api/analyze?symbol=sh600519&period=day"   # period=day|m30|m15
curl -X POST "http://127.0.0.1:8787/api/market/scan?scope=all"        # 全部A股（剔除北交所与ST，5028只）
curl -X POST "http://127.0.0.1:8787/api/market/scan?scope=hs300"      # 仅沪深300，秒级验证用
curl -X POST "http://127.0.0.1:8787/api/market/scan?scope=all&force=true"  # 忽略缓存强制重取
curl "http://127.0.0.1:8787/api/market/candidates?recent_days=30"
curl "http://127.0.0.1:8787/api/market/pools?days=3"      # 买点池
curl "http://127.0.0.1:8787/api/market/risk?days=3"       # 卖点风险池
curl "http://127.0.0.1:8787/api/signals/history?period=m15&limit=20"
curl "http://127.0.0.1:8787/api/watchlist"                    # 只在圈的状态（一买/二买失败已出圈）
curl "http://127.0.0.1:8787/api/watchlist/stats"              # 一买/二买成功率 + 进出圈计数
curl "http://127.0.0.1:8787/api/market/success-rate?symbol=sh600000"  # 该股自身口径
curl -X POST "http://127.0.0.1:8787/api/watchlist/refresh"    # 本地缓存重评关注圈（不联网，约1-2秒）
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

**gzip 不是可选项。** `/api/market/candidates` 一次返回 7 MB JSON，不开压缩时
在 ~1 Mbps 出口上要传 58 秒；开了之后是 0.36 MB / 3 秒。这是首页从 20 秒级降到
5 秒级的**唯一最大杠杆**，务必按下面配置。

#### 6.1 全局 gzip（推荐，一次覆盖 80/443 所有 server）

```bash
sudo tee /etc/nginx/conf.d/gzip.conf > /dev/null <<'EOF'
gzip on;
gzip_comp_level 6;
gzip_min_length 1024;
gzip_vary on;
gzip_proxied any;
gzip_types
    text/plain
    text/css
    text/xml
    text/javascript
    application/json
    application/javascript
    application/xml
    application/xml+rss
    application/w3c
    image/svg+xml;
EOF
```

先确认 `nginx.conf` 的 `http {}` 里有这一行（Debian/Ubuntu 默认有）：

```bash
grep -n 'conf.d' /etc/nginx/nginx.conf
# 期望：include /etc/nginx/conf.d/*.conf;
```

> ⚠️ **`application/json` 必须显式列出**：nginx 默认 `gzip_types` 只含 `text/html`，
> 不写这一项，API 的 JSON 完全不会被压缩——响应头会出现 `Vary: Accept-Encoding`
> 却没有 `Content-Encoding`，看起来像"开了"，实际没生效。
>
> ⚠️ **不要写 `text/html`**：它默认总被压缩，重复声明会报 `duplicate MIME type` 警告。
>
> ⚠️ **`gzip_proxied any` 别省**：以后若在前面加 CDN 或再加一层代理，请求会带 `Via`
> 头，缺这个参数就不压缩了。

#### 6.2 server 块

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
        expires 30d;                  # 自带 Cache-Control: max-age，不要再 add_header 一次
        gzip_static on;               # 可选：优先发预压缩的 .gz（见 6.3）
    }
}
```

> `gzip_static` 需要 nginx 编译了 `ngx_http_gzip_static_module`（Debian/Ubuntu 官方包默认含）。
> 若 `nginx -t` 报 `unknown directive "gzip_static"`，直接删掉这一行即可——
> 没做 6.3 的预压缩时它本来也不起作用。

> 若不用 6.1 的全局配置，而是把 gzip 指令直接写进各 server 块，
> **443 的 server 块必须同样加一份**，否则 HTTPS 访问不压缩。

#### 6.3 可选：静态文件预压缩

`echarts.min.js` 有 1 MB 且在 `<head>` 里加载，预压缩可省掉每次动态压缩的 CPU：

```bash
gzip -9 -k /opt/chanpicker/frontend/vendor/echarts.min.js
# 生成同目录 echarts.min.js.gz，配合上面的 gzip_static on 生效
```

#### 6.4 应用配置并验证

```bash
sudo nginx -t && sudo systemctl reload nginx

# API 是否压缩（content-length 应从 7002365 降到 30 万量级）
curl -sI -H 'Accept-Encoding: gzip' http://your.domain.com/api/market/candidates \
  | grep -iE 'content-encoding|content-length'

# 静态资源是否压缩（应从 1029203 降到 33 万量级）
curl -sI -H 'Accept-Encoding: gzip' http://your.domain.com/static/vendor/echarts.min.js \
  | grep -iE 'content-encoding|content-length'
```

期望两条都出现 `content-encoding: gzip`。

> 本配置已用 `nginx:1.31.7-alpine` 实测验证（2026-10-20）：
> 经 `proxy_pass` 转发、响应带 `Cache-Control: no-store` 的 `application/json`
> 场景下，14,600 字节压缩到 1,125 字节并正确返回 `Content-Encoding: gzip`。
> **`/api/*` 的 `no-store` 不会阻止压缩**——两者互不影响，这一点不必顾虑。

#### 6.5 HTTPS

```bash
sudo certbot --nginx -d your.domain.com
```

> certbot 生成的 443 server 块会自动沿用 6.1 的全局 gzip；若改用 6.2 的写法，
> 记得给新块补一份 gzip 指令。

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
# 升级（先备份数据库，多周期改造会改动表结构）
sudo -u chanpicker cp /var/lib/chanpicker/chanpicker.db /var/lib/chanpicker/chanpicker.db.bak-$(date +%F)
sudo -u chanpicker bash -c 'cd /opt/chanpicker && git pull && .venv/bin/pip install -e .'
sudo systemctl restart chanpicker
curl http://127.0.0.1:8787/api/health        # 看 cached 里的 day/m30/m15 是否正常

# 回滚
sudo -u chanpicker git -C /opt/chanpicker checkout <上一个 commit>
sudo systemctl restart chanpicker
```

数据目录在 `/var/lib/chanpicker/`，不受代码回滚影响。

**多周期改造的库迁移（自动、幂等）**：本版本给 `bars` / `signals` 加了 `period` 列并重建唯一约束。服务**首次启动时会自动迁移**，把存量数据回填为 `period='day'`（实测 13 万行 K 线 + 1700 条信号耗时约 0.5 秒）。这一步是必须的——否则旧缓存会被判定为「当前周期无数据」，触发全量重新拉取，撞上数据源限流。

升级后还有一件事：**旧信号的库里没有卖点记录，需重新执行一次市场扫描**（`POST /api/market/scan`）才会生成一卖/二卖，卖点风险池才会非空。当日已有日线缓存时，这次扫描是本地重算，秒级完成。

**2026-10 口径统一版的升级动作（重要）**：

```bash
# 1) 备份（scan_hits 会重建表）
sudo -u chanpicker cp /var/lib/chanpicker/chanpicker.db /var/lib/chanpicker/chanpicker.db.bak-$(date +%F)

# 2) 拉代码并重启（scan_hits 主键迁移在启动时自动跑，幂等）
sudo -u chanpicker bash -c 'cd /opt/chanpicker && git pull && .venv/bin/pip install -e .'
sudo systemctl restart chanpicker

# 3) 重建结局样本（池子/选股页的体系分依赖它；全市场约 25 秒）
curl -X POST http://127.0.0.1:8787/api/system/rebuild-outcomes

# 4) 重评关注圈（把僵尸记录归档为「二买守住」，让新一买能开新一轮；约 10 秒）
curl -X POST http://127.0.0.1:8787/api/watchlist/refresh
```

不做 3) 的话，「三日买卖点池」的胜算列会全部显示「—」、「体系选股」会提示「体系尚未构建」——
这两个功能都依赖 `signal_outcomes` + `system_snapshots`，空表时只能诚实报空，不会编数据。

不做 4) 的话，关注圈里仍会残留「一买日期在几百天前」的僵尸记录（它们要等下一次扫描
走到该股才会被重评），关注圈数量与买点池的差值会偏大。

**验证清单**（部署后逐条确认）：

```bash
# ① 三个买点视图在相同窗口下必须完全相等（3 个数字应两两相同）
curl -s "http://127.0.0.1:8787/api/market/candidates?recent_days=3" | python3 -c "import sys,json;d=json.load(sys.stdin);print('candidates',len(d['first_buy']),len(d['second_buy']),len(d['third_buy']))"
curl -s "http://127.0.0.1:8787/api/market/pools?days=3"          | python3 -c "import sys,json;d=json.load(sys.stdin);print('pools     ',len(d['first_buy']),len(d['second_buy']),len(d['third_buy']))"
curl -s "http://127.0.0.1:8787/api/system/pick?days=3&min_score=-1&limit=500" | python3 -c "import sys,json;d=json.load(sys.stdin);print('pick total',d.get('total_scored'))"

# ② 池子每行都要有体系分（system_available 全为 true）
curl -s "http://127.0.0.1:8787/api/market/pools?days=3" | python3 -c "import sys,json;d=json.load(sys.stdin);rows=d['first_buy']+d['second_buy']+d['third_buy'];print('有分',sum(1 for r in rows if r['system_available']),'/',len(rows))"

# ③ scan_hits 主键已含 trade_date
sqlite3 /var/lib/chanpicker/chanpicker.db "PRAGMA index_list(scan_hits);"
```

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
# 3) 单只个股端到端（应返回 qualifies / risk_active / bars）
curl -s "http://127.0.0.1:8787/api/analyze?symbol=sh600519&period=day" | head -c 300
# 4) 候选与关注圈
curl -s "http://127.0.0.1:8787/api/market/candidates?recent_days=30" | head -c 300
curl -s http://127.0.0.1:8787/api/watchlist | head -c 300
curl -s http://127.0.0.1:8787/api/watchlist/stats | head -c 300      # 成功率 + 进出圈计数
curl -X POST http://127.0.0.1:8787/api/watchlist/refresh | head -c 300  # 本地重评（不联网）
# 4b) 服务端性能自检（在本机 127.0.0.1 上测，排除带宽与代理因素）
for ep in "/api/market/pools?days=3" "/api/market/risk?days=3" "/api/watchlist" \
          "/api/market/candidates" "/api/watchlist/stats"; do
  curl -s -o /dev/null -w "%{time_total}s %{size_download}B  $ep\n" "http://127.0.0.1:8787$ep"
done
# 参考值（本机热缓存）：watchlist ~0.1s / risk ~0.2s / pools ~0.8s /
# candidates ~2s（冷库首次 ~6s）/ stats ~0.04s
# candidates 超过 3s 说明胜率计算在重复跑；其余接口 >1s 说明索引丢了

# 4c) 判断「慢」发生在服务端还是带宽：两边各测一次对比
DOMAIN="stock.temeu.top"          # 换成你的域名
curl -s -o /dev/null -w "本机 TTFB=%{time_starttransfer}s 总=%{time_total}s 速度=%{speed_download}B/s\n" \
  "http://127.0.0.1:8787/api/market/candidates"
curl -s -o /dev/null -w "公网 TTFB=%{time_starttransfer}s 总=%{time_total}s 速度=%{speed_download}B/s\n" \
  "http://${DOMAIN}/api/market/candidates"

# 判读规则：
#   本机 TTFB 就很大              → 服务端计算/DB 慢，查索引与重复计算
#   两边 TTFB 接近、公网总耗时远大于本机
#                                 → 带宽瓶颈，必须开 gzip（6.1 节）；换数据库无用
#   公网 speed 长期低于 100 KB/s  → 出口约 1 Mbps，压缩是唯一有效手段
#   两边都很快                    → 慢在前端串行请求或浏览器渲染，查 Network 时间线

# 5) 首次全市场扫描（无缓存约 2–5 分钟；有当日缓存秒级返回）
curl -X POST "http://127.0.0.1:8787/api/market/scan?scope=all"        # 全部A股（剔除北交所与ST，5028只）
curl -X POST "http://127.0.0.1:8787/api/market/scan?scope=hs300"      # 仅沪深300，秒级验证用
curl -X POST "http://127.0.0.1:8787/api/market/scan?scope=all&force=true"  # 忽略缓存强制重取
curl -s http://127.0.0.1:8787/api/market/scan/status | head -c 200
# 6) 多周期（分钟数据源端有限流，失败会自动退避重试）
curl -s "http://127.0.0.1:8787/api/analyze?symbol=sh600519&period=m15" | head -c 200
# 7) 买卖点池（升级后需先跑过一次扫描，卖点池才非空）
curl -s "http://127.0.0.1:8787/api/market/pools?days=3" | head -c 200
curl -s "http://127.0.0.1:8787/api/market/risk?days=3"  | head -c 200
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
| 卖点风险池一直是空的 | 升级后还没重新扫描；旧信号表里没有卖点记录 | 执行一次 `POST /api/market/scan?scope=all`（有当日缓存时秒级完成） |
| 全市场扫描要跑很久（30–50 分钟） | 数据源每次最多 20 只、且有限流；首次全量无缓存可复用 | 正常现象。看进度条上的「预计剩余」；中断后重跑会跳过已取数据。建议放夜间定时任务 |
| 扫描范围比预期少几百只 | 已按口径剔除北交所（350 只）与 ST/*ST（198 只） | 属预期行为；`scope=all` 响应里的 `universe` 字段给出「数据源总数 / 剔除北交所 / 剔除ST / 保留」四项明细 |
| 磁盘被数据库撑大 | 全量 K 线约 220 万根 | 数据库约 450–550 MB。备份只保留最近 2 份；`data/*.bak-*` 可安全清理 |
| **页面打开要等十几秒才出数据** | 旧版本 `bars` 索引前导列是 symbol，而 `recent_trading_dates`/`period_counts` 只按 period 过滤 → 全扫 218 万行 | 拉取最新代码（已加 `(period, trade_date)` 覆盖索引），重启后自动补建，实测 8990 ms → 0.24 ms |
| **索引已修、首页仍要 20 秒以上** | **nginx 没开 gzip**。`/api/market/candidates` 返回 7 MB，在 ~1 Mbps 出口上传输 58 秒；实测服务端首字节只用了 4.8 秒 | 按第 6.1 节配 gzip，**`gzip_types` 必须含 `application/json`**。开完 7 MB → 0.36 MB，传输降到 3 秒级。详见 `改造方案-首页查询性能优化.md` |
| 首页接口并发时互相拖慢（单个很快、一起就慢） | `Store` 是单条 SQLite 连接 + 一把 `RLock` 串行化，最慢的请求会把其余请求堵在后面（实测 `risk` 单跑 0.22 s → 并发 3.98 s） | 属已知架构约束。优化方向见方案 P1（缓存去重）与 P2-3（WAL + 只读连接池），后者触碰 `@_sync` 约束需单独验证 |
| `/static/vendor/echarts.min.js` 单独就要 7–8 秒 | 1.03 MB 且在 `<head>` 同步加载，阻塞首屏渲染 | 加 `defer`（图表初始化本就在 DOM 就绪后）+ 第 6.3 节预压缩 + `expires 30d` |
| 升级后首次启动卡十几秒 | 在给 218 万行的老库建新索引 | 只此一次，等它跑完即可，之后启动不受影响 |
| 升级后首次启动卡几秒到几十秒 | 在给 `signals` 表去重（历史上同信号被重复插入过）并重建唯一约束 | 只此一次。日志可见删除行数；去重后个股历史次数会回落到真实值 |
| 个股成功率显示 0% 但印象里这只票经常出二买 | 历史数据被 `days` 参数污染成重复，同一信号被算成多次尝试 | 拉取最新代码（已重建 `signals` 唯一约束并清理存量），升级后数字恢复真实 |
| 个股成功率显示「样本不足」 | 该股历史信号 < 3 次，统计上给百分比没有意义（1/1=100% 会误导） | 正常表现。等样本积累到 3 次才显示百分比，期间会列出逐次明细 |
| 表格里想按别的字段排 | 表头未声明排序键 | 代码/名称/各日期/各价格/各强度列均可点表头排序；「状态」「类型」「操作」三列不参与排序 |
| 30分钟 / 15分钟 报「有效K线不足」或数据很少 | 数据源对分钟周期只保留约 1 个月，实测 30 分钟约 72 根、15 分钟约 96 根 | 正常现象，不是故障。分钟级别只用于区间套确认与日内择时，选股请用日线 |
| 分析分钟周期时偶发「请求过于频繁」 | 数据源限流（实测连续取数会触发） | 已内置 3 次退避重试（20s/40s 递增）；若仍失败，等 1 分钟再试 |
| 页面数据错乱（列表为空、出现空行） | 旧版本共享 sqlite 连接存在并发读串扰 | 拉取最新代码重启服务（已为所有库访问加锁）；回归用例见 `tests/test_backend_store.py` |
| **打开首页一片空白，点一下页签才有数据** | 旧版本只给导航按钮加 `active`、没给对应 `<section>` 加，而 CSS 是 `section.tab { display:none }`，四个区块全被隐藏（数据其实已请求回来） | 拉取最新代码重启服务（页签切换已收敛为唯一入口 `switchTab()`，并在初始化时调用）；静态契约回归见 `tests/test_frontend_contract.py` |
| 关注圈只剩两三种状态，历史「一买失败」的都不见了 | 属预期：一买失败/二买失败即**剔除出圈**（记录仍在库里） | 看工具栏「已出圈 M 只」计数，或 `GET /api/watchlist/stats` 的 `removed` 字段 |
| 二买成功率一开始是 100%，看着不可信 | 老数据里机会态从未被重评过，没有失败样本 | 点「重评关注圈」跑一次本地重评（约 1~2 秒），失败样本才会被统计出来 |
| 重评后有些「机会已触发」没变成二买失败 | 那些二买已超过 `max_wait_bars`（30 根）考验窗口仍未破位，按口径记为守住 | 属预期。窗口之外的破位可能是后续大盘整体回落，不计入这笔二买的失败 |
| 服务重启后扫描进度一直卡在 `running` 不动 | 旧版本进程被 kill 时 `finish_run` 来不及执行，`scan_runs` 残留 running 行 | 拉取最新代码（启动时会把残留 running 统一判为 `interrupted`）；回归用例见 `tests/test_watchlist_service.py::StaleRunRecoveryTest` |

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

---

## 定时任务

两个任务错峰跑，互不阻塞：

```cron
# 1) 日线全市场扫描（约 30-50 分钟）
0 18 * * 1-5 cd /opt/chanpicker && .venv/bin/python scripts/daily_scan.py >> /var/log/chanpicker/scan.log 2>&1

# 2) 关注圈 30 分钟增量拉取（约 41 分钟）
#    用途：为 p100 买点定律积累次级别数据（详见「分钟数据的能力边界」）
30 17 * * 1-5 cd /opt/chanpicker && .venv/bin/python scripts/sync_intraday.py >> /var/log/chanpicker/intraday.log 2>&1
```

### 分钟数据的能力边界（重要，别误判）

实测 westock 的分钟数据**只有约 10 个交易日**：

| 周期 | 允许查询跨度 | 实际能拿到 |
|---|---|---|
| m5 | 10 天 | 约 10 个交易日 |
| m15 | 20 天 | 约 10 个交易日 |
| m30 | 30 天 | 约 10 个交易日（81 根） |

「30 天」是**允许你查多早**的许可，**不是有这么多数据**。用 30 天窗口请求，
实际只回 `2026-09-28 ~ 2026-10-16`。

由此推论：

- `sync_intraday.py` 每天跑，**只能让未来的二买具备 p100 验证条件**；
- 存量二买里约 **89%** 落在数据窗口外，**永久补不回**；
- p100 递归还需要 30 分钟上形成「两个同向中枢」（p116「没有趋势没有背驰」），
  约需 3 个交易日，所以新二买是 **T+3 起**可验证，稳态可验证率 ≈ 100%；
- 分钟 K 线**强制不复权**（日线是 qfq），跨周期对比时口径不同，注意。

**不要把这个脚本当成「补历史数据」的工具**——它做不到。

### 若确实需要历史分钟数据

westock 满足不了，只能引入外部数据源（AkShare / TuShare / 付费源）。
那是独立的依赖变更，需要评估接口稳定性与授权方式。

---

## 免责声明

本系统为技术分析研究工具，所有信号基于公开行情数据与缠论量化规则推导，仅供参考，不构成投资建议。市场有风险，投资需谨慎。过往表现不预示未来收益。
