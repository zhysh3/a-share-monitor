#!/usr/bin/env python3
"""update_arisk_data.py — 每日收盘后生成 /home/zhuya/Desktop/arisk_data.json

由 cron 在交易日 16:10 调用。优先 MX API（社融存量同比），失败回退 AKShare M2 同比。
所有 section 独立 try/except；某段失败时复用旧 JSON 对应字段，整体不退出。
"""
import json, os, sys, time, traceback
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed

OUT_PATH = os.environ.get('ARISK_OUT') or os.path.join(os.path.dirname(os.path.abspath(__file__)), 'arisk_data.json')
MX_KEY = os.environ.get('MX_APIKEY', '')

def log(msg): print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)

# ── 老 JSON 兜底 ─────────────────────────────────────────────
def load_prev():
    try:
        with open(OUT_PATH) as f: return json.load(f)
    except Exception: return {}
PREV = load_prev()

def fallback(key, default=None):
    v = PREV.get(key)
    if v is None: return default
    log(f"  ↩ {key} 复用旧值")
    return v

# ── 1. 社融存量同比 ──────────────────────────────────────────
# 主源：央行（PBoC）官网『社会融资规模存量统计表』，其“增速（%）”列即社融存量同比。
#       央行口径、最权威，每月约15日发布上月数据，比商务部镜像（AKShare shrzgm）更及时。
# 回退：① 商务部镜像增量累计（旧法，口径偏高约1pp且滞后）② M2 同比（IC≈0，占位）。
TSF_BASELINE_201412 = 1228600   # 122.86 万亿 = 1,228,600 亿（央行 2015-01 货政报告，旧法基准）

PBOC_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
           "(KHTML, like Gecko) Chrome/120 Safari/537.36")
PBOC_HOST = "https://www.pbc.gov.cn"

def _pboc_decode(b):
    for enc in ("utf-8", "gbk", "gb18030"):
        try: return b.decode(enc)
        except Exception: continue
    return b.decode("utf-8", "ignore")

def _pboc_cells(row_html):
    import re
    cs = re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", row_html, re.S | re.I)
    return [re.sub(r"<[^>]+>", "", c).replace("&nbsp;", " ").replace("\xa0", " ").strip()
            for c in cs]

def pboc_year_tsf(year):
    """抓央行某年『社会融资规模存量统计表』，返回 [{'m':'YY-MM','g':同比,'s':'pboc'}]，仅含已发布月份。"""
    import re, requests
    h = {"User-Agent": PBOC_UA, "Accept-Language": "zh-CN,zh;q=0.9"}
    idx_url = f"{PBOC_HOST}/diaochatongjisi/116219/116319/{year}ntjsj/shrzgm/index.html"
    idx = _pboc_decode(requests.get(idx_url, headers=h, timeout=20).content)
    pos = idx.find("社会融资规模存量统计表")          # 标签后第一个 attachDir htm 即该表
    if pos < 0:
        raise RuntimeError("年度索引未见『社会融资规模存量统计表』")
    m = re.search(r"/diaochatongjisi/attachDir/\d{4}/\d{2}/\d+\.htm", idx[pos:pos + 600])
    if not m:
        raise RuntimeError("未找到存量统计表 htm 链接")
    tbl = _pboc_decode(requests.get(PBOC_HOST + m.group(0), headers=h, timeout=20).content)
    months = re.findall(r"(20\d\d)\.(\d{1,2})", tbl)          # 表头月份，按序
    total = None                                              # 合计行：首格含“社会融资规模存量”+“AFRE”
    for row in re.findall(r"<tr[^>]*>(.*?)</tr>", tbl, re.S | re.I):
        c = _pboc_cells(row)
        if c and c[0].startswith("社会融资规模存量") and "AFRE" in c[0]:
            total = c; break
    if not total:
        raise RuntimeError("未找到社会融资规模存量合计行")
    pairs = list(zip(total[1::2], total[2::2]))               # (存量, 增速) ×12
    out = []
    for (yy, mm), (_stock, yoy) in zip(months, pairs):
        try: g = float(yoy)
        except Exception: continue
        if 0 < g < 30:
            out.append({"m": f"{yy[2:]}-{int(mm):02d}", "g": round(g, 2), "s": "pboc"})
    return out

def _merge_pboc(fresh):
    """新抓的央行月份并入历史中已是央行口径(s=pboc)的月份，按月去重排序取后12个。
    刻意不混入旧累计法的月份，避免在口径接缝处产生虚假的一阶导跳变。"""
    cached = {e['m']: e for e in (PREV.get('m2_monthly') or [])
              if isinstance(e, dict) and e.get('s') == 'pboc'}
    for e in fresh:
        cached[e['m']] = e
    return sorted(cached.values(), key=lambda e: e['m'])[-12:]

def fetch_credit_yoy():
    # ── 主源：央行官方社融存量同比（最权威、最及时）──
    try:
        from datetime import date
        yr = date.today().year
        rows = pboc_year_tsf(yr)
        # 始终尝试拉上一年，避免图上只剩当年那几根柱子（历史 2025 数据也需要）
        try: rows = pboc_year_tsf(yr - 1) + rows
        except Exception as e: log(f"  · PBoC 上年表不可得: {e}")
        rows = _merge_pboc(rows)
        if len(rows) >= 3:
            log(f"  ✓ 社融存量同比[央行口径] ({len(rows)} 月) 最新 {rows[-1]}  · PBoC 直连")
            return rows
        log(f"  ✗ PBoC 社融存量同比 点数不足: {len(rows)} 月")
    except Exception as e:
        log(f"  ✗ PBoC 社融存量同比 失败: {e}")
        traceback.print_exc()
    # ── 回退1：商务部镜像 社融增量累计（旧法，口径偏高约1pp且滞后）──
    try:
        import akshare as ak
        df = ak.macro_china_shrzgm()
        df = df.copy()
        df['月份'] = df['月份'].astype(str)
        df = df[df['月份'].str.match(r'^\d{6}$')].sort_values('月份').reset_index(drop=True)
        # 累计存量 = 基准 + cumsum 增量
        df['增量'] = df['社会融资规模增量'].astype(float)
        df['存量'] = TSF_BASELINE_201412 + df['增量'].cumsum()
        # 12 月同比
        df['stock_lag12'] = df['存量'].shift(12)
        df['yoy'] = (df['存量'] / df['stock_lag12'] - 1) * 100
        df = df.dropna(subset=['yoy'])
        out = []
        for _, r in df.tail(14).iterrows():
            ym = r['月份']
            out.append({"m": f"{ym[2:4]}-{ym[4:6]}", "g": round(float(r['yoy']), 2)})
        if len(out) >= 6:
            log(f"  ⚠ 社融存量同比[回退·商务部镜像累计] ({len(out)} 月) 最新 {out[-1]}  · 口径偏高~1pp且滞后")
            return out[-12:]
        log(f"  ✗ 社融存量同比[回退·累计] 数据不足: {len(out)} 月")
    except Exception as e:
        log(f"  ✗ 社融存量同比[回退·累计] 失败: {e}")
        traceback.print_exc()
    # 回退：M2 同比（IC≈0，仅占位）
    try:
        import akshare as ak
        df = ak.macro_china_money_supply()
        date_col = next(c for c in df.columns if '月' in c or 'date' in c.lower())
        val_col = next(c for c in df.columns if 'M2' in c and '同比' in c)
        df = df.sort_values(date_col)
        out = []
        for _, row in df.tail(14).iterrows():
            raw = str(row[date_col]).strip()
            digits = ''.join(c for c in raw if c.isdigit())
            if len(digits) < 6: continue
            yy, mm = digits[2:4], digits[4:6]
            try: g = round(float(str(row[val_col]).replace('%','')), 2)
            except Exception: continue
            if 0 < g < 30: out.append({"m": f"{yy}-{mm}", "g": g})
        if len(out) >= 6:
            log(f"  ⚠ M2 同比兜底（信号 IC≈0）最新 {out[-1]}")
            return out[-12:]
    except Exception as e:
        log(f"  ✗ M2 也失败: {e}")
    return fallback('m2_monthly')

# ── 2. 10Y 国债 ──────────────────────────────────────────
def fetch_bond10y():
    try:
        import akshare as ak
        df = ak.bond_zh_us_rate()
        col = next(c for c in df.columns if '中国' in c and '10年' in c and '差' not in c)
        df = df[['日期', col]].dropna().sort_values('日期').tail(30)
        hist = [{"d": f"{d.month}/{d.day}", "v": round(float(v), 4)}
                for d, v in zip(__import__('pandas').to_datetime(df['日期']), df[col])]
        latest = round(float(df[col].iloc[-1]), 2)
        log(f"  ✓ bond10y latest={latest}% ({len(hist)} 天)")
        return {"latest": latest, "hist": hist}
    except Exception as e:
        log(f"  ✗ bond10y 失败: {e}")
        return fallback('bond10y')

# ── 3. 沪深300 PE ─────────────────────────────────────────
def fetch_pe_300():
    try:
        import akshare as ak
        df = ak.stock_index_pe_lg(symbol='沪深300')
        pe = round(float(df.iloc[-1]['滚动市盈率']), 2)
        log(f"  ✓ pe_300 = {pe}")
        return pe
    except Exception as e:
        log(f"  ✗ pe_300 失败: {e}")
        return fallback('pe_300')

# ── 4. HS300 HV30 ──────────────────────────────────────────
def fetch_hv30():
    try:
        import akshare as ak, math
        # Sina 源稳定，东财接口偶尔 RemoteDisconnected
        df = ak.stock_zh_index_daily(symbol="sh000300")
        df = df.sort_values('date').reset_index(drop=True)
        closes = df['close'].astype(float).tolist()
        dates = df['date'].astype(str).tolist()
        # 30 日年化 HV
        hv_series = []
        for i in range(30, len(closes)):
            window = closes[i-30:i+1]
            log_rets = [math.log(window[j]/window[j-1]) for j in range(1, len(window))]
            mean = sum(log_rets)/len(log_rets)
            var = sum((r-mean)**2 for r in log_rets)/len(log_rets)
            hv_series.append(round(math.sqrt(var)*math.sqrt(252)*100, 1))
        hist_dates = dates[30:]
        # 仅保留最近 30 天作 hist
        out_hist = [{"d": f"{int(d[5:7])}/{int(d[8:10])}", "v": v}
                    for d, v in zip(hist_dates[-30:], hv_series[-30:])]
        latest = hv_series[-1]
        # 5 年分位（用最近 ~1250 个交易日的 HV 分布）
        recent5y = hv_series[-1250:] if len(hv_series) >= 1250 else hv_series
        below = sum(1 for v in recent5y if v <= latest)
        pct = round(below / len(recent5y) * 100)
        log(f"  ✓ hv30 latest={latest}% pct={pct}% (基于 {len(recent5y)} 个交易日)")
        return {"latest": latest, "pct": pct, "hist": out_hist}
    except Exception as e:
        log(f"  ✗ hv30 失败: {e}")
        traceback.print_exc()
        return fallback('hv30')

# ── 5. 两融 daily + monthly ─────────────────────────────────
def fetch_margin():
    try:
        import akshare as ak
        from datetime import timedelta
        today = datetime.now()
        start = (today - timedelta(days=400)).strftime('%Y%m%d')
        end = today.strftime('%Y%m%d')
        sse = ak.stock_margin_sse(start_date=start, end_date=end)
        sse_col = next((c for c in sse.columns if '余额' in c and '融资融券' in c), None) or '融资融券余额'
        date_col = next(c for c in sse.columns if '日期' in c)
        sse = sse[[date_col, sse_col]].rename(columns={date_col:'date', sse_col:'sse'})
        sse['date'] = sse['date'].astype(str).str[:8]
        try:
            szse = ak.stock_margin_szse(date=end)
            log(f"  · szse 单日点对点查询行数: {len(szse)}")
        except Exception:
            szse = None
        # 取 SSE 作为主，深圳合计 ×1.85（经验比；避免 szse 多日接口不稳）
        sse_all = sse.sort_values('date').reset_index(drop=True)
        sse_all['sse'] = sse_all['sse'].astype(float)
        sse_all['total'] = sse_all['sse'] * 1.85
        # daily 取最近 30 天
        sse_daily = sse_all.tail(30)
        daily = [{"d": f"{int(d[4:6])}/{int(d[6:8])}", "v": int(round(float(v)/1e8))}
                 for d, v in zip(sse_daily['date'].tolist(), sse_daily['total'].tolist())]
        # monthly 用全部数据按 YYYY-MM 分组取月内最后一日
        # 关键：剔除"当月未完成"月份，避免月中值当"月末"用，导致 Z 分数失真
        sse_all['ym'] = sse_all['date'].str[:6]
        current_ym = today.strftime('%Y%m')
        last_by_month = sse_all.groupby('ym').last().reset_index()
        # 只保留 ym < 当前月 的"已完成"月份
        completed = last_by_month[last_by_month['ym'] < current_ym]
        monthly = [{"m": f"{r['ym'][2:4]}-{r['ym'][4:6]}", "v": int(round(r['total']/1e8))}
                   for _, r in completed.iterrows()]
        monthly = monthly[-12:]
        # 单独把"当月至今"记录到 current_month（不参与 Z 计算，但方便 dashboard 显示）
        cur_row = last_by_month[last_by_month['ym'] == current_ym]
        current_month = None
        if not cur_row.empty:
            r = cur_row.iloc[0]
            current_month = {"m": f"{r['ym'][2:4]}-{r['ym'][4:6]}",
                             "v": int(round(r['total']/1e8)),
                             "partial": True,
                             "as_of": daily[-1]['d'] if daily else None}
        log(f"  ✓ margin daily={len(daily)} monthly={len(monthly)}(已完成) "
            f"{'+当月未完成 ' + current_month['m'] if current_month else ''}"
            f"最新日余额 {daily[-1]['v']} 亿")
        out = {"daily": daily[-30:], "monthly": monthly[-12:]}
        if current_month: out["current_month"] = current_month
        return out
    except Exception as e:
        log(f"  ✗ margin 失败: {e}")
        return fallback('margin')

# ── 6. 近 7 日成交额 ────────────────────────────────────────
def fetch_vol_7d():
    """新浪源 stock_zh_index_daily + 经验换算系数（同 proxy.py 实现，规避东财 TLS 限制）"""
    try:
        import akshare as ak
        AMT_FACTOR = {"sh000001": 19.1, "sz399001": 20.8}
        def _close_vol(sym):
            df = ak.stock_zh_index_daily(symbol=sym).sort_values('date').tail(7).reset_index(drop=True)
            df['date'] = df['date'].astype(str)
            return df
        sh = _close_vol("sh000001")
        sz = _close_vol("sz399001")
        n = min(len(sh), len(sz))
        out = []
        for i in range(n):
            d = sh.iloc[i]['date']
            # 估算成交额（亿）= close × volume(股) × factor / 1e8
            amt_sh = float(sh.iloc[i]['close']) * float(sh.iloc[i]['volume']) * AMT_FACTOR['sh000001'] / 1e8
            amt_sz = float(sz.iloc[i]['close']) * float(sz.iloc[i]['volume']) * AMT_FACTOR['sz399001'] / 1e8
            # 上面那个估算偏大；真正实测：amt ≈ volume(股) × avgPrice ≈ volume × close / 100
            # 实际：沪深两市日成交≈ 1-2 万亿，volume sh000001 在 60-80 亿股，close ~4000 → close*vol=2.5e14
            # 简化：实际 amount/volume ratio 实测大概是 19-21 (元/股 平均价位)
            # 用经验系数：amt = volume × factor (yuan)，factor 取上面 AMT_FACTOR
            amt_sh = float(sh.iloc[i]['volume']) * AMT_FACTOR['sh000001'] / 1e8
            amt_sz = float(sz.iloc[i]['volume']) * AMT_FACTOR['sz399001'] / 1e8
            total = amt_sh + amt_sz
            out.append({"d": f"{int(d[5:7])}/{int(d[8:10])}", "v": int(round(total))})
        log(f"  ✓ vol_7d {len(out)} 天（新浪估算），最新 {out[-1]['v']} 亿")
        return out
    except Exception as e:
        log(f"  ✗ vol_7d 失败: {e}")
        traceback.print_exc()
        return fallback('vol_7d')

# ── 7. 近 5 日涨跌停 ─────────────────────────────────────
def fetch_limit_7d():
    try:
        import akshare as ak
        from datetime import timedelta
        # 取近 10 个自然日找出 5 个交易日
        out, dt = [], datetime.now()
        for _ in range(15):
            ds = dt.strftime('%Y%m%d')
            try:
                up = len(ak.stock_zt_pool_em(date=ds))
                dn = len(ak.stock_zt_pool_dtgc_em(date=ds))
                if up > 0 or dn > 0:
                    out.append({"date": dt.strftime('%Y-%m-%d'), "up": up, "down": dn})
                    if len(out) >= 5: break
            except Exception: pass
            dt -= timedelta(days=1)
        out.reverse()
        log(f"  ✓ limit_7d {len(out)} 天，今 up/dn={out[-1]['up']}/{out[-1]['down']}")
        return out
    except Exception as e:
        log(f"  ✗ limit_7d 失败: {e}")
        return fallback('limit_7d')

# ── 8. 申万一级 60 日涨跌 ────────────────────────────────────
def fetch_sector_live():
    try:
        import akshare as ak
        info = ak.sw_index_first_info()
        items = [(row['行业代码'].split('.')[0], row['行业名称']) for _, row in info.iterrows()]

        def _one(code, name):
            try:
                df = ak.index_hist_sw(symbol=code, period='day')
                df = df.sort_values('日期').reset_index(drop=True)
                closes = df['收盘'].astype(float).tolist()
                if len(closes) < 61: return None
                ret60 = (closes[-1]/closes[-61]-1)*100
                today = (closes[-1]/closes[-2]-1)*100
                return {"n": name, "code": code,
                        "excess": round(today, 2), "ret60": round(ret60, 2),
                        "date": str(df['日期'].iloc[-1])[:10]}
            except Exception: return None

        out = []
        with ThreadPoolExecutor(max_workers=8) as ex:
            futs = {ex.submit(_one, c, n): (c, n) for c, n in items}
            for f in as_completed(futs):
                r = f.result()
                if r: out.append(r)
        out.sort(key=lambda x: x['ret60'], reverse=True)
        log(f"  ✓ sector_live {len(out)}/{len(items)} 行业，最强 {out[0]['n']} {out[0]['ret60']}%")
        return out
    except Exception as e:
        log(f"  ✗ sector_live 失败: {e}")
        return fallback('sector_live')

# ── 9. 全 A 换手率（沪+深合并）─────────────────────────────
# 用于 dashboard L2 pcaCrowding，之前 dashboard 只能靠 estimateTurn(volYuan) 粗估
def _turnover_one_day(d):
    """单日全A换手率。d = 'YYYYMMDD'。失败抛异常。"""
    import akshare as ak
    sh = ak.stock_sse_deal_daily(date=d)
    sz = ak.stock_szse_summary(date=d)
    if sh.empty or sz.empty:
        raise ValueError("empty frame")
    # 沪：主板 A + 科创板（单位已是亿元）
    sh_amt_row = sh[sh['单日情况'] == '成交金额']
    sh_cap_row = sh[sh['单日情况'] == '流通市值']
    sh_amt = float(sh_amt_row['主板A'].iloc[0]) + float(sh_amt_row['科创板'].iloc[0])
    sh_cap = float(sh_cap_row['主板A'].iloc[0]) + float(sh_cap_row['科创板'].iloc[0])
    # 深：主板 A + 创业板 A（单位是元，需 /1e8）
    sz_a = sz[sz['证券类别'].isin(['主板A股', '创业板A股'])]
    sz_amt = float(sz_a['成交金额'].sum()) / 1e8
    sz_cap = float(sz_a['流通市值'].sum()) / 1e8
    total_amt, total_cap = sh_amt + sz_amt, sh_cap + sz_cap
    return {
        "date": f"{d[:4]}-{d[4:6]}-{d[6:8]}",
        "label": f"{int(d[4:6])}/{int(d[6:8])}",
        "sh_amount_yi": round(sh_amt),
        "sz_amount_yi": round(sz_amt),
        "sh_mktcap_yi": round(sh_cap),
        "sz_mktcap_yi": round(sz_cap),
        "amount_yi": round(total_amt),
        "mktcap_yi": round(total_cap),
        "pct": round(total_amt / total_cap * 100, 3),
    }

def fetch_turnover():
    """全A换手率最近 7 个交易日序列 = 沪深合计成交额 / 沪深合计A股流通市值 × 100。

    返回 {..最新日字段.., 'avg_pct': 最新值, 'series': [{label,date,pct,amount_yi}, ...×7]}
    series 供 dashboard 主面板"两市成交额×换手率"图表使用（此前该图只能用
    硬编码 9e13 流通市值估算，与真实流通市值有约 8% 偏差）。
    """
    try:
        from datetime import timedelta
        days, cursor = [], datetime.now()
        # 往回扫最多 20 个自然日，凑齐 7 个交易日
        for _ in range(20):
            d = cursor.strftime('%Y%m%d')
            try:
                days.append(_turnover_one_day(d))
                if len(days) >= 7: break
            except Exception:
                pass
            cursor -= timedelta(days=1)
        if not days:
            log("  ✗ turnover 20 天内无可用交易日数据")
            return fallback('turnover')
        days.reverse()                       # 由旧到新
        latest = days[-1]
        out = dict(latest)
        out['avg_pct'] = latest['pct']       # 向后兼容旧字段名
        out['series'] = [{"label": x['label'], "date": x['date'],
                          "pct": x['pct'], "amount_yi": x['amount_yi']} for x in days]
        log(f"  ✓ turnover {latest['date']} = {latest['pct']}% "
            f"（{latest['amount_yi']}亿/{latest['mktcap_yi']}亿），序列 {len(days)} 日")
        return out
    except Exception as e:
        log(f"  ✗ turnover 失败: {e}")
        traceback.print_exc()
    return fallback('turnover')

# ── 10. 偏股基金新发 ───────────────────────────────────────
def fetch_fund_issuance():
    try:
        import akshare as ak
        df = ak.fund_new_found_em()
        col_date = next(c for c in df.columns if '成立' in c or '日期' in c)
        col_share = next(c for c in df.columns if '份额' in c)
        col_type = next((c for c in df.columns if '类型' in c), None)
        if col_type:
            df = df[df[col_type].astype(str).str.contains('股票|混合', na=False)]
        df = df.dropna(subset=[col_date, col_share]).copy()
        import pandas as pd
        df[col_date] = pd.to_datetime(df[col_date], errors='coerce')
        df = df.dropna(subset=[col_date])
        df['ym'] = df[col_date].dt.strftime('%y-%m')
        df[col_share] = pd.to_numeric(df[col_share], errors='coerce')
        agg = df.groupby('ym')[col_share].sum().sort_index().tail(12)
        out = [{"m": ym, "v": round(float(v), 1)} for ym, v in agg.items()]
        log(f"  ✓ fund_issuance {len(out)} 月，最新 {out[-1] if out else '空'}")
        return out
    except Exception as e:
        log(f"  ✗ fund_issuance 失败: {e}")
        return fallback('fund_issuance')

# ── 11. ETF 资金分类流向（沪市，60交易日净流入）───────────────
# 数据源：上交所 ETF 份额（ak.fund_etf_scale_sse，按 STAT_DATE 取快照），
#   现价来自 ak.fund_etf_spot_em。净流入 ≈ (份额_now − 份额_60d前) × 现价。
#   旧份额按现价计值以隔离价格因素，change_pct = 净流入/旧市值。
#   分类由基金简称关键词判定，行业主题优先于宽基，「其他」占比约 0~2%。
ETF_RULES = [
    ("增强指数", ["增强"]),
    ("跨境",     ["恒生","恒指","恒","中概","港股","H股","HK","HKC","纳指","纳斯达克","标普","日经",
                  "德国","DAX","道琼斯","海外","美股","东南亚","亚太","越南","印度","法国","沙特",
                  "新兴市场","全球","中韩","日本","欧洲","香港","东证","NA股","沪港深","港科","巴西",
                  "新兴亚洲","亚洲","中金优","MSCI中国","富时中国"]),
    ("商品",     ["黄金","白银","原油","豆粕","能源化工","农产品","大宗","饲料","生猪期","有色金属期",
                  "上海金","金ETF","商品"]),
    ("债券货币", ["可转债","转债","国债","政金债","信用债","城投债","货币","债ETF","短融","国开","地方债",
                  "现金基金","现金指数","现金ETF","中银现金","活期","添益","现金添","债"]),
    ("半导体芯片",["芯片","半导","存储","封测","集成电路","科创芯","芯","科创材料","科创新材","电子"]),
    ("AI算力",   ["人工智能","算力","云计算","大数据","数字经济","机器人","数据中心","AI","数据","数字"]),
    ("软件通信", ["软件","计算机","信创","网络安全","通信","5G","物联网","游戏","传媒","互联网","网络",
                  "云","信息","TMT","科技","文娱","影视","电信"]),
    ("医药生物", ["医药","医疗","创新药","疫苗","中药","基因","生物","疫","CXO","器械","医",
                  "新药","生科","保健","养老","药"]),
    ("新能源电力",["新能源","电池","锂电","光伏","储能","风电","电网","电力","绿电","核电","氢能",
                  "新能车","碳中和","新能","双碳","公用","能源","低碳","绿色"]),
    ("汽车交运", ["汽车","整车","零部件","物流","运输","港口","航运","铁路","公路","交通","交运","车",
                  "智能驾驶","驾驶"]),
    ("消费",     ["白酒","食品","饮料","家电","家居","旅游","免税","零售","纺织","服装","农业","养殖",
                  "消费","畜牧","乳","酒","农牧","宠物","美容","农林牧渔","教育","消服","消电","国货"]),
    ("金融地产", ["银行","证券","券商","保险","地产","REIT","金融","房","不动产"]),
    ("军工制造", ["军工","国防","航空","航天","卫星","船舶","机械","装备","专精特新","工业母机",
                  "高端制造","兵","导弹","国防军工","智能制造","智造","通航"]),
    ("周期资源", ["有色","煤炭","钢铁","化工","矿","稀土","稀有","石油","石化","建材","水泥","资源",
                  "材料","钢","煤","油气","电解铝","锂","环保","金属","新材","基建"]),
    ("风格因子", ["红利","低波","价值","成长","质量","动量","ESG","自由现金流","现金流","现金自由",
                  "自由现金","基本面","央企","国企","国资","央创","龙头","蓝筹","分红","股息","价值回报",
                  "可持续","央调","央视"]),
    ("宽基",     ["沪深300","中证500","上证50","中证1000","中证2000","A500","中证A50","科创50",
                  "科创100","科创综","创业板","上证综指","上证指数","深证","中证100","中证800","双创",
                  "国证","巨潮","中证全指","上证180","上证380","MSCI","富时","综指","规模",
                  "治理","超大","中盘","大盘","小盘","上证","中证","沪深","全指","战略新兴","产业升级",
                  "长三角","湾区","G60","之江","综合","龙头股","央视50","长江","张江","科创","科综","科200","A股",
                  "300","500","1000","2000","50","800","180","100","380","225","580"]),
]

def _etf_classify(name):
    for cat, kws in ETF_RULES:
        for kw in kws:
            if kw in name:
                return cat
    return "其他"

def _sse_scale_on(date_str):
    """某交易日沪市 ETF 份额 DataFrame；空/失败返回 None。date_str='YYYYMMDD'"""
    try:
        import akshare as ak
        df = ak.fund_etf_scale_sse(date=date_str)
        return df if (df is not None and len(df) > 100) else None
    except Exception:
        return None

def _sse_find_valid(anchor, back_days):
    """从 anchor 向前最多 back_days 天找一个 SSE 有数的日子，返回 (df, 'YYYY-MM-DD')"""
    from datetime import timedelta
    cur = anchor
    for _ in range(back_days):
        df = _sse_scale_on(cur.strftime('%Y%m%d'))
        if df is not None:
            return df, cur.strftime('%Y-%m-%d')
        cur -= timedelta(days=1)
    return None, None

def fetch_etf_categories():
    try:
        import akshare as ak
        from datetime import timedelta
        from collections import defaultdict
        # 现价
        spot = ak.fund_etf_spot_em()
        price = {str(r['代码']): float(r['最新价']) for _, r in spot.iterrows()
                 if r['最新价'] and float(r['最新价']) > 0}
        # 最新 + 约60交易日前 两个份额快照
        df_now, date_now = _sse_find_valid(datetime.now(), 8)
        if df_now is None:
            log("  ✗ etf_categories: SSE 最新份额不可得")
            return fallback('etf_categories')
        anchor_old = datetime.strptime(date_now, '%Y-%m-%d') - timedelta(days=88)
        df_old, date_old = _sse_find_valid(anchor_old, 12)
        old_share = ({str(r['基金代码']): float(r['基金份额']) for _, r in df_old.iterrows()}
                     if df_old is not None else {})

        agg = defaultdict(lambda: {"count": 0, "scale_now": 0.0, "scale_old": 0.0, "flow": 0.0})
        for _, r in df_now.iterrows():
            code = str(r['基金代码']); name = str(r['基金简称'])
            p = price.get(code)
            if not p:
                continue
            sh_now = float(r['基金份额'])
            sh_old = old_share.get(code, sh_now)      # 新上市无旧值 → 计 0 流入
            scale_now = sh_now * p / 1e8              # 亿元
            scale_old = sh_old * p / 1e8              # 旧份额按现价计值
            a = agg[_etf_classify(name)]
            a["count"] += 1; a["scale_now"] += scale_now
            a["scale_old"] += scale_old; a["flow"] += (scale_now - scale_old)

        cats = []
        for cat, a in agg.items():
            pct = (a["flow"] / a["scale_old"] * 100) if a["scale_old"] > 0 else 0.0
            cats.append({"category": cat, "count_now": a["count"],
                         "scale_yi": round(a["scale_now"], 1),
                         "change_yi": round(a["flow"], 1),
                         "change_pct": round(pct, 2)})
        cats.sort(key=lambda x: x["change_yi"], reverse=True)
        total = sum(c["scale_yi"] for c in cats) or 1
        other = next((c["scale_yi"] for c in cats if c["category"] == "其他"), 0)
        log(f"  ✓ etf_categories now={date_now} vs {date_old} "
            f"{len(cats)}类/{sum(c['count_now'] for c in cats)}只，其他占比 {other/total*100:.1f}%")
        return {"latest_date": date_now, "prev_date": date_old, "categories": cats}
    except Exception as e:
        log(f"  ✗ etf_categories 失败: {e}")
        traceback.print_exc()
        return fallback('etf_categories')

# ── 主流程 ────────────────────────────────────────────────
def main():
    t0 = time.time()
    log("=== update_arisk_data.py 开始 ===")
    log(f"  MX_APIKEY: {'已配置' if MX_KEY else '未配置（仅 AKShare 回退）'}")

    out = {
        "generated_at": datetime.now().strftime('%Y-%m-%dT%H:%M:%S'),
        "generated_date": datetime.now().strftime('%Y-%m-%d'),
    }

    log("[1/11] 抓 社融存量同比 / M2 ...")
    out['m2_monthly'] = fetch_credit_yoy()
    log("[2/11] 抓 10Y 国债 ...")
    out['bond10y'] = fetch_bond10y()
    log("[3/11] 抓 沪深300 PE ...")
    out['pe_300'] = fetch_pe_300()
    log("[4/11] 算 HV30 ...")
    out['hv30'] = fetch_hv30()
    log("[5/11] 抓 两融 ...")
    out['margin'] = fetch_margin()
    log("[6/11] 抓 近7日成交额 ...")
    out['vol_7d'] = fetch_vol_7d()
    log("[7/11] 抓 近5日涨跌停 ...")
    out['limit_7d'] = fetch_limit_7d()
    log("[8/11] 抓 申万31行业60日 ...")
    out['sector_live'] = fetch_sector_live()
    log("[9/11] 抓 全A 换手率 ...")
    out['turnover'] = fetch_turnover()
    log("[10/11] 抓 偏股基金新发 ...")
    out['fund_issuance'] = fetch_fund_issuance()
    log("[11/11] 抓 ETF 资金分类流向（沪市60日）...")
    out['etf_categories'] = fetch_etf_categories()

    # 保留 None 的字段（fallback 拿不到时）但记录
    missing = [k for k, v in out.items() if v is None]
    if missing: log(f"⚠ 以下字段缺失: {missing}")

    # 写入
    tmp = OUT_PATH + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    os.replace(tmp, OUT_PATH)
    log(f"=== 完成（{time.time()-t0:.1f}s），输出 {OUT_PATH} ===")

if __name__ == '__main__':
    sys.exit(main())
