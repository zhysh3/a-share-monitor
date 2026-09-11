# A股风险监视器 · A-RISK/MONITOR

一个本地运行的 A 股大盘风险监测看板：ERP 股权风险溢价、万得全A PE、10Y 国债、破净率、两市成交额×换手率、HV30 波动率、信贷脉冲（社融存量同比一阶导）、两融余额+动量、ETF 资金流向、申万行业热力图，以及一个「两层漏斗决策模型」给出综合仓位建议。

数据每交易日收盘后自动抓取（AKShare + 央行官网直连 + 新浪/东财），本地静态页面渲染，**无需任何后端服务器**。

> ⚠️ 本项目仅供研究学习，所有指标不构成投资建议。投资有风险，入市需谨慎。

## 组成

| 文件 | 作用 |
|---|---|
| `arisk_monitor_local.html` | 看板本体（Chart.js 走 CDN，其余内联） |
| `update_arisk_data.py` | 抓全量数据 → 生成 `arisk_data.json`（约 95 秒） |
| `proxy.py` | 本地代理(8899)，供浏览器盘中实时抓数 + 妙想API 转发 |
| `check_and_update.sh` | 判断数据是否落后于最新交易日，落后才更新 |
| `run_arisk_update.sh` | 跑一次更新（被 check 调用，或手动） |
| `start.sh` / `stop.sh` | 一键起停（代理 8899 + 静态服务器 8788） |
| `arisk_data.json` | 数据快照（仓库内为种子数据，跑一次更新即刷新） |

## 快速开始

```bash
git clone <你的仓库地址> arisk
cd arisk

# 1. 建虚拟环境 + 装依赖（需 Python 3.9+）
python3 -m venv venv
./venv/bin/pip install -r requirements.txt

# 2.（可选）配妙想 API key；不配也能跑，社融走央行直连
cp .env.example .env
#   然后编辑 .env 填入 MX_APIKEY

# 3. 首次抓数
./venv/bin/python update_arisk_data.py

# 4. 启动并打开看板
bash start.sh
```

看板地址：<http://localhost:8788/arisk_monitor_local.html>

> ⚠️ **必须通过 `start.sh`（本地 http）打开，不能直接双击 HTML**——`file://` 协议下浏览器禁止读取本地 JSON，页面会空白。

停止服务：`bash stop.sh`

## 每日自动更新

数据只在**交易日收盘后（约 18:00 起）**发布，`check_and_update.sh` 会判断当前数据是否已覆盖最新交易日：已覆盖则秒退，落后才抓。

- **macOS（launchd）**：见 `com.arisk.update.plist.example`，把 `__ARISK_DIR__` 换成本目录绝对路径后装入 `~/Library/LaunchAgents/`，每天 16:10–22:10 每小时判断一次。
- **Linux（cron）**：`crontab -e` 添加
  ```
  10 16-22 * * * /bin/bash /path/to/arisk/check_and_update.sh
  ```

手动立即更新：`bash run_arisk_update.sh`

## 已知限制

- **数据源在中国境内**（东财/新浪/央行）。海外服务器直连可能受限或较慢，`proxy.py` 已用 `curl_cffi` 模拟 Chrome TLS 指纹绕过部分反爬；仍不通时需自行加代理。
- 本仓库的 `proxy.py` 为**通用反代版**，未实现看板期望的部分语义端点（`/pe` `/bond` `/index_kline` 等）。这些盘中 live-refresh 会回退到 `arisk_data.json`——由于该 JSON 每日自动更新，数据整体仍是新的，只是缺分钟级盘中刷新。
- 妙想 API（`MX_APIKEY`）为可选增强，缺省走 AKShare/央行回退。

## 安全

`.env` 含你的 API key，已被 `.gitignore` 排除。**切勿把真实 `.env` 提交或分享。** 如误提交，请立即在东财后台吊销并更换 key。
