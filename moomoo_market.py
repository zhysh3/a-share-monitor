#!/usr/bin/env python3
"""moomoo_market.py — moomoo OpenAPI（OpenD 网关）行情适配层

本项目的**行情数据**（指数日 K、成交额、ETF 现价、快照）统一走 moomoo OpenD，
AKShare / 新浪 / 东财仅作回退。宏观数据（社融、国债、两融、申万行业）moomoo
不提供，仍走原有数据源。

前置条件：
  1. pip install moomoo-api            （或 futu-api，本模块两者都兼容）
  2. 本机（或内网）运行 moomoo OpenD，登录后开放 11111 端口
  3. 账号具备沪深行情权限（LV1 即可满足日 K + 快照）

环境变量：
  MOOMOO_ENABLED   0/false 关闭 moomoo，全部回退到 AKShare（默认开启）
  MOOMOO_HOST      OpenD 地址，默认 127.0.0.1
  MOOMOO_PORT      OpenD 端口，默认 11111

设计约定：**任何失败都返回 None 而不抛异常**，调用方据此回退，
OpenD 没开也不影响整条更新链路。
"""

import os
import threading
import time
from datetime import datetime, timedelta

# ── 配置 ────────────────────────────────────────────────────
HOST = os.environ.get('MOOMOO_HOST', '127.0.0.1')
PORT = int(os.environ.get('MOOMOO_PORT', '11111') or 11111)
ENABLED = str(os.environ.get('MOOMOO_ENABLED', '1')).lower() not in ('0', 'false', 'no', 'off')

# 连接失败后的冷却时间：OpenD 没开时避免每次调用都卡 TCP 超时
_FAIL_COOLDOWN = 60.0
# get_market_snapshot 单次上限 400，留余量分批
_SNAPSHOT_BATCH = 200

_lock = threading.RLock()
_sdk = None                 # 已导入的 SDK 模块（moomoo 或 futu）
_sdk_name = None
_sdk_tried = False
_ctx = None                 # OpenQuoteContext 单例
_last_fail_ts = 0.0
_last_error = None


def _log(msg):
    print(f"[moomoo] {msg}", flush=True)


# ── SDK / 连接 ──────────────────────────────────────────────
def _load_sdk():
    """导入 moomoo（优先）或 futu SDK，失败返回 None。"""
    global _sdk, _sdk_name, _sdk_tried, _last_error
    if _sdk_tried:
        return _sdk
    _sdk_tried = True
    err = None
    for name in ('moomoo', 'futu'):
        try:
            _sdk = __import__(name)
            _sdk_name = name
            return _sdk
        except ImportError:
            continue
        except SystemExit as e:
            # SDK 的 __init__ 缺依赖时会直接 sys.exit(1)，不能让它带走整个进程
            err = f"{name} SDK 依赖缺失（sys.exit {e.code}）"
            break
        except Exception as e:
            err = f"{name} SDK 导入失败: {type(e).__name__}: {e}"
            break
    _last_error = err or "未安装 moomoo-api（pip install moomoo-api）"
    return None


def _context():
    """返回可用的 OpenQuoteContext，不可用返回 None。"""
    global _ctx, _last_fail_ts, _last_error
    if not ENABLED:
        _last_error = "MOOMOO_ENABLED=0，已禁用"
        return None
    with _lock:
        if _ctx is not None:
            return _ctx
        if time.time() - _last_fail_ts < _FAIL_COOLDOWN:
            return None                      # 冷却中，不重复尝试
        sdk = _load_sdk()
        if sdk is None:
            _last_fail_ts = time.time()
            return None
        try:
            ctx = sdk.OpenQuoteContext(host=HOST, port=PORT)
            # 探活：拿一次全局状态，OpenD 没登录会直接报错
            ret, data = ctx.get_global_state()
            if ret != sdk.RET_OK:
                raise RuntimeError(str(data))
            _ctx = ctx
            _last_error = None
            _log(f"已连接 OpenD {HOST}:{PORT}（SDK={_sdk_name}）")
            return _ctx
        except Exception as e:
            _last_error = f"{type(e).__name__}: {e}"
            _last_fail_ts = time.time()
            _log(f"连接 OpenD {HOST}:{PORT} 失败 → 回退 AKShare（{_last_error}）")
            return None


def close():
    """关闭 OpenD 连接（脚本退出前调用，避免 SDK 后台线程挂住进程）。"""
    global _ctx
    with _lock:
        if _ctx is not None:
            try:
                _ctx.close()
            except Exception:
                pass
            _ctx = None


def available():
    """moomoo 行情当前是否可用。"""
    return _context() is not None


def status():
    """供日志/健康检查用的状态字典。"""
    ok = available()
    return {
        "enabled": ENABLED,
        "sdk": _sdk_name,
        "host": HOST,
        "port": PORT,
        "ok": ok,
        "error": None if ok else _last_error,
    }


# ── 代码规范化 ──────────────────────────────────────────────
def normalize_code(sym):
    """把项目里各种写法统一成 moomoo 的 'SH.000300' 形式。

    支持：'sh000300' / 'SH.000300' / '000300.SH' / '510300'（纯 6 位按前缀猜市场）
    无法判定时返回 None。
    """
    if not sym:
        return None
    s = str(sym).strip().upper().replace(' ', '')
    if '.' in s:
        a, b = s.split('.', 1)
        if a in ('SH', 'SZ'):
            return f"{a}.{b}"
        if b in ('SH', 'SZ'):
            return f"{b}.{a}"
        return None
    if s.startswith(('SH', 'SZ')) and len(s) > 2:
        return f"{s[:2]}.{s[2:]}"
    if s.isdigit() and len(s) == 6:
        # 6/9/5/68 → 沪市（含科创板、沪市 ETF）；0/1/2/3 → 深市
        return f"{'SH' if s[0] in '569' else 'SZ'}.{s}"
    return None


# ── 日 K ────────────────────────────────────────────────────
def daily_kline(sym, days=120, autype='qfq'):
    """取日 K（升序）。

    返回 [{'date':'YYYY-MM-DD','open','close','high','low','volume','turnover',
           'turnover_rate','change_rate'}, ...]；失败返回 None。
    turnover 是**真实成交额（元）**，比新浪源 volume×经验系数 的估算准确得多。
    """
    code = normalize_code(sym)
    if code is None:
        return None
    ctx = _context()
    if ctx is None:
        return None
    sdk = _sdk
    # 日历日按 1.55 倍换算交易日，再多留 10 天缓冲
    span = int(days * 1.55) + 10
    start = (datetime.now() - timedelta(days=span)).strftime('%Y-%m-%d')
    end = datetime.now().strftime('%Y-%m-%d')
    au = {'qfq': sdk.AuType.QFQ, 'hfq': sdk.AuType.HFQ, 'none': sdk.AuType.NONE}.get(
        str(autype).lower(), sdk.AuType.QFQ)
    try:
        ret, df, _ = ctx.request_history_kline(
            code, start=start, end=end, ktype=sdk.KLType.K_DAY,
            autype=au, max_count=None)
        if ret != sdk.RET_OK:
            _log(f"{code} 日 K 失败: {df}")
            return None
        if df is None or len(df) == 0:
            return None
    except Exception as e:
        _log(f"{code} 日 K 异常: {type(e).__name__}: {e}")
        _drop_context()
        return None

    def _f(row, key):
        try:
            v = row.get(key)
            return None if v is None else float(v)
        except Exception:
            return None

    out = []
    for _, r in df.iterrows():
        out.append({
            "date": str(r.get('time_key', ''))[:10],
            "open": _f(r, 'open'), "close": _f(r, 'close'),
            "high": _f(r, 'high'), "low": _f(r, 'low'),
            "volume": _f(r, 'volume'), "turnover": _f(r, 'turnover'),
            "turnover_rate": _f(r, 'turnover_rate'),
            "change_rate": _f(r, 'change_rate'),
        })
    out = [x for x in out if x['date'] and x['close']]
    out.sort(key=lambda x: x['date'])
    return out[-days:] if days else out


def daily_closes(sym, days=120):
    """日 K 收盘序列 → ([日期...], [收盘...])；失败返回 None。"""
    kl = daily_kline(sym, days=days)
    if not kl:
        return None
    return [k['date'] for k in kl], [k['close'] for k in kl]


def _drop_context():
    """连接出错后丢弃单例，下次调用重连（带冷却）。"""
    global _ctx, _last_fail_ts
    with _lock:
        if _ctx is not None:
            try:
                _ctx.close()
            except Exception:
                pass
            _ctx = None
        _last_fail_ts = time.time()


def _plain(v):
    """numpy 标量 → 原生 Python 类型（否则 json.dumps 会报 not serializable）。"""
    item = getattr(v, 'item', None)
    if callable(item) and not isinstance(v, (str, bytes)):
        try:
            return v.item()
        except Exception:
            pass
    return v


# ── 快照 ────────────────────────────────────────────────────
def snapshot(syms):
    """批量快照 → {'SH.600000': {...}}；失败返回 None，部分批次失败则跳过该批。"""
    codes = [c for c in (normalize_code(s) for s in syms) if c]
    if not codes:
        return {}
    ctx = _context()
    if ctx is None:
        return None
    sdk = _sdk
    out = {}
    for i in range(0, len(codes), _SNAPSHOT_BATCH):
        batch = codes[i:i + _SNAPSHOT_BATCH]
        try:
            ret, df = ctx.get_market_snapshot(batch)
            if ret != sdk.RET_OK:
                _log(f"快照失败（{len(batch)} 只）: {df}")
                continue
            for _, r in df.iterrows():
                out[str(r['code'])] = {k: _plain(v) for k, v in dict(r).items()}
        except Exception as e:
            _log(f"快照异常: {type(e).__name__}: {e}")
            _drop_context()
            return out or None
        if i + _SNAPSHOT_BATCH < len(codes):
            time.sleep(0.6)          # get_market_snapshot 限频 60 次/30 秒
    return out or None


def last_prices(syms):
    """{'600000': 最新价}（键为不带市场前缀的 6 位代码）；失败返回 None。"""
    snap = snapshot(syms)
    if not snap:
        return None
    out = {}
    for code, row in snap.items():
        try:
            p = float(row.get('last_price') or 0)
        except Exception:
            continue
        if p > 0:
            out[code.split('.')[-1]] = p
    return out or None


if __name__ == '__main__':
    import json
    print(json.dumps(status(), ensure_ascii=False))
    kl = daily_kline('sh000300', days=5)
    print(json.dumps(kl, ensure_ascii=False, indent=2) if kl else "日 K 不可用")
    close()
