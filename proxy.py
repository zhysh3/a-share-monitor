#!/usr/bin/env python3
"""
A股风险监测 - 本地代理服务器

行情主源是 **moomoo OpenAPI**（本机 moomoo OpenD 网关，见 moomoo_market.py）：
盘中的指数 K 线 / 成交额 / 快照都从 OpenD 取，不再依赖东财、新浪的反爬接口。
OpenD 没开或没有行情权限时，自动回退到原来的新浪/东财通用反代（下面这套）。

push2his.eastmoney.com 有 TLS 指纹检测，普通 requests 会被断连（RemoteDisconnected）。
本代理对 push2his 使用 curl_cffi 模拟 Chrome124 TLS 指纹，其余域名用 requests。

依赖：
    pip install moomoo-api requests curl_cffi

运行：python proxy.py
"""

from http.server import HTTPServer, BaseHTTPRequestHandler
import urllib.parse
import urllib.request
import json
import re
import sys

# ── requests（普通域名） ──────────────────────────────────────
try:
    import requests
    from requests.adapters import HTTPAdapter
    HAS_REQUESTS = True
except ImportError:
    HAS_REQUESTS = False

# ── curl_cffi（push2his TLS 指纹绕过） ───────────────────────
try:
    from curl_cffi import requests as cf_requests
    HAS_CURL_CFFI = True
except ImportError:
    HAS_CURL_CFFI = False

# ── moomoo 行情适配层 ───────────────────────────────────────
try:
    import moomoo_market as mm
    HAS_MOOMOO = True
except Exception as _e:            # 适配层本身导入失败也不能拖垮代理
    mm = None
    HAS_MOOMOO = False
    print(f"[Proxy] moomoo_market 导入失败: {_e}", flush=True)

PORT = 8899

# 新浪回退估算成交额用的经验系数（元/股 均价），与 update_arisk_data.py 保持一致
AMT_FACTOR = {"sh000001": 19.1, "sz399001": 20.8}

ALLOWED_HOSTS = [
    "hq.sinajs.cn",
    "money.finance.sina.com.cn",   # HV30历史K线
    "push2.eastmoney.com",
    "82.push2.eastmoney.com",
    "datacenter-web.eastmoney.com",
    "push2his.eastmoney.com",
    "stock.gtimg.cn",
    "qt.gtimg.cn",
]

# push2his 需要 curl_cffi 模拟 Chrome TLS，普通 requests 会被 RemoteDisconnected
CURL_CFFI_HOSTS = ["push2his.eastmoney.com"]

REFERER_MAP = {
    "sinajs.cn":     "https://finance.sina.com.cn/",
    "eastmoney.com": "https://quote.eastmoney.com/",
    "gtimg.cn":      "https://gu.qq.com/",
}

BROWSER_HEADERS = {
    "User-Agent":      "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept":          "application/json, text/plain, */*",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection":      "keep-alive",
    "Sec-Fetch-Dest":  "empty",
    "Sec-Fetch-Mode":  "cors",
    "Sec-Fetch-Site":  "same-site",
}

# requests Session（普通域名）
session = None
if HAS_REQUESTS:
    session = requests.Session()
    adapter = HTTPAdapter(max_retries=2)
    session.mount("https://", adapter)
    session.mount("http://", adapter)


def get_referer(url):
    for key, ref in REFERER_MAP.items():
        if key in url:
            return ref
    return "https://finance.sina.com.cn/"


def needs_curl_cffi(host):
    return any(host.endswith(h) for h in CURL_CFFI_HOSTS)


class ProxyHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        print(f"[Proxy] {args[0]} {args[1]}", flush=True)

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        params = urllib.parse.parse_qs(parsed.query)

        # ── moomoo 行情端点（盘中实时，OpenD 直连）──
        if parsed.path == "/index_kline":
            self._index_kline(params)
            return
        if parsed.path == "/quote":
            self._quote(params)
            return
        if parsed.path == "/health":
            self._json(self._health())
            return

        # ── 语义端点：直接用每日更新的 arisk_data.json 供数据 ──
        # 看板通过这些端点判断"是否实时"(detectEnv 探测 /pe)并读取当日数据；
        # moomoo 给不了的宏观字段（社融/国债/两融/行业）从这里出。
        semantic = self._semantic(parsed.path)
        if semantic is not None:
            self._json(semantic)
            return

        if "url" not in params:
            self._error(400, "Missing ?url= parameter")
            return

        target_url = urllib.parse.unquote(params["url"][0])
        target_host = urllib.parse.urlparse(target_url).netloc

        if not any(target_host.endswith(h) for h in ALLOWED_HOSTS):
            self._error(403, f"Host not allowed: {target_host}")
            return

        try:
            headers = {**BROWSER_HEADERS, "Referer": get_referer(target_url)}

            if needs_curl_cffi(target_host):
                # curl_cffi：模拟 Chrome124 TLS 指纹，绕过 push2his 反爬
                if not HAS_CURL_CFFI:
                    self._error(503, "curl_cffi not installed. Run: pip install curl_cffi")
                    return
                resp = cf_requests.get(
                    target_url,
                    headers=headers,
                    impersonate="chrome124",
                    timeout=15,
                )
                data = resp.content
                content_type = resp.headers.get("Content-Type", "text/plain; charset=utf-8")

            elif HAS_REQUESTS and session:
                # requests：普通域名
                resp = session.get(target_url, headers=headers, timeout=15)
                data = resp.content
                content_type = resp.headers.get("Content-Type", "text/plain; charset=utf-8")

            else:
                # urllib 兜底（已在顶层 import，无局部变量冲突）
                req = urllib.request.Request(target_url, headers=headers)
                with urllib.request.urlopen(req, timeout=15) as r:
                    data = r.read()
                    content_type = r.headers.get("Content-Type", "text/plain; charset=utf-8")

            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            self.wfile.write(data)

        except Exception as e:
            print(f"[Proxy] Error: {target_url} → {e}", flush=True)
            self._error(502, str(e))

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/mx":
            self._mx_proxy()
        else:
            self._error(404, "Not found")

    def _mx_proxy(self):
        import os
        api_key = os.environ.get("MX_APIKEY", "")
        if not api_key:
            self._error(503, "MX_APIKEY not set. Run: export MX_APIKEY=your_key")
            return
        try:
            content_length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(content_length)
            mx_url = "https://mkapi2.dfcfs.com/finskillshub/api/claw/query"
            headers = {"Content-Type": "application/json", "apikey": api_key}

            if HAS_CURL_CFFI:
                resp = cf_requests.post(mx_url, content=body, headers=headers, timeout=30)
                data = resp.content
                content_type = resp.headers.get("Content-Type", "application/json")
            elif HAS_REQUESTS and session:
                resp = session.post(mx_url, data=body, headers=headers, timeout=30)
                data = resp.content
                content_type = resp.headers.get("Content-Type", "application/json")
            else:
                req = urllib.request.Request(mx_url, data=body, headers=headers, method="POST")
                with urllib.request.urlopen(req, timeout=30) as r:
                    data = r.read()
                    content_type = r.headers.get("Content-Type", "application/json")

            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            self.wfile.write(data)
        except Exception as e:
            print(f"[Proxy] MX Error: {e}", flush=True)
            self._error(502, str(e))

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def _error(self, code, msg):
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(json.dumps({"error": msg}).encode())

    def _json(self, obj):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(body)

    # ── moomoo 行情端点 ─────────────────────────────────────
    def _index_kline(self, params):
        """GET /index_kline?code=sh000300&days=60
        → [{"d":"YYYY-MM-DD","close":..,"amt":成交额(元),"vol":成交量(股)}, ...] 升序

        主源 moomoo OpenD（amt 为真实成交额）；回退新浪日 K（amt 用经验系数估算）。
        """
        code = (params.get("code") or ["sh000300"])[0]
        try:
            days = max(1, min(1500, int((params.get("days") or ["60"])[0])))
        except ValueError:
            days = 60

        if HAS_MOOMOO:
            kl = mm.daily_kline(code, days=days)
            if kl:
                self._json([{"d": k["date"], "close": k["close"],
                             "amt": k["turnover"], "vol": k["volume"],
                             "src": "moomoo"} for k in kl])
                return

        out = self._sina_kline(code, days)
        if out is None:
            self._error(502, f"index_kline 不可用（moomoo OpenD 未连接，新浪回退也失败）: {code}")
            return
        self._json(out)

    def _sina_kline(self, code, days):
        """新浪日 K 回退：成交额用 volume × 经验系数估算。失败返回 None。"""
        if not re.fullmatch(r"(?:sh|sz)\d{6}", code.lower()):
            print(f"[Proxy] 非法 index_kline code: {code}", flush=True)
            return None
        code = code.lower()
        url = ("https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/"
               f"CN_MarketData.getKLineData?symbol={code}&scale=240&ma=no&datalen={days}")
        try:
            body = self._raw_get(url)
            rows = json.loads(body.decode("utf-8", "ignore").replace("'", '"'))
        except Exception as e:
            print(f"[Proxy] 新浪日K回退失败 {code} → {e}", flush=True)
            return None
        factor = AMT_FACTOR.get(code.lower(), 0)
        out = []
        for r in rows:
            try:
                vol = float(r.get("volume") or 0)
                out.append({"d": str(r["day"])[:10], "close": float(r["close"]),
                            "amt": (vol * factor) if factor else None,
                            "vol": vol, "src": "sina"})
            except Exception:
                continue
        return out or None

    def _quote(self, params):
        """GET /quote?code=sh000300,510300 → {code: {price, open, prev_close, pct, ...}}"""
        raw = (params.get("code") or [""])[0]
        codes = [c for c in raw.replace(" ", "").split(",") if c]
        if not codes:
            self._error(400, "Missing ?code=")
            return
        if not HAS_MOOMOO:
            self._error(503, "moomoo_market 不可用（pip install moomoo-api）")
            return
        snap = mm.snapshot(codes)
        if not snap:
            self._error(502, "moomoo OpenD 未连接或无行情权限")
            return
        out = {}
        for code, r in snap.items():
            prev = r.get("prev_close_price") or 0
            last = r.get("last_price") or 0
            out[code] = {
                "price": last,
                "open": r.get("open_price"),
                "high": r.get("high_price"),
                "low": r.get("low_price"),
                "prev_close": prev,
                "pct": round((last / prev - 1) * 100, 2) if prev else None,
                "volume": r.get("volume"),
                "amount": r.get("turnover"),
                "turnover_rate": r.get("turnover_rate"),
                "pe_ttm": r.get("pe_ttm_ratio"),
                "pb": r.get("pb_ratio"),
                "update_time": r.get("update_time"),
            }
        self._json(out)

    def _health(self):
        st = mm.status() if HAS_MOOMOO else {"ok": False, "error": "moomoo_market 未导入"}
        return {"proxy": "ok", "port": PORT, "moomoo": st,
                "requests": HAS_REQUESTS, "curl_cffi": HAS_CURL_CFFI}

    def _raw_get(self, url):
        """通用 GET，返回 bytes（供回退源使用）。"""
        headers = {**BROWSER_HEADERS, "Referer": get_referer(url)}
        if HAS_CURL_CFFI:
            return cf_requests.get(url, headers=headers, impersonate="chrome124",
                                   timeout=15).content
        if HAS_REQUESTS and session:
            return session.get(url, headers=headers, timeout=15).content
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.read()

    def _semantic(self, path):
        """看板语义端点 → 直接读同目录每日更新的 arisk_data.json。
        返回 None 表示不是语义端点（交回通用 ?url= 反代处理）。"""
        import os
        routes = {"/pe", "/bond", "/prebuilt", "/sectors", "/margin", "/sf", "/fund",
                  "/etf_categories", "/turnover"}
        if path not in routes:
            return None
        data_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "arisk_data.json")
        try:
            with open(data_file, encoding="utf-8") as f:
                d = json.load(f)
        except Exception:
            d = {}
        if path == "/pe":
            return {"pe": d.get("pe_300"), "pb": None}
        if path == "/bond":
            b = d.get("bond10y") or {}
            return {"yield": b.get("latest"), "hist": b.get("hist", [])}
        if path == "/prebuilt":
            return d
        if path == "/sectors":
            return d.get("sector_live", [])
        if path == "/margin":
            return (d.get("margin") or {}).get("monthly", [])
        if path == "/sf":
            return d.get("m2_monthly", [])
        if path == "/fund":
            return d.get("fund_issuance", [])
        if path == "/etf_categories":
            return d.get("etf_categories", {})
        if path == "/turnover":
            return d.get("turnover", {})
        return None


if __name__ == "__main__":
    req_status  = "已安装" if HAS_REQUESTS  else "未安装 → pip install requests"
    cffi_status = "已安装" if HAS_CURL_CFFI else "未安装 → pip install curl_cffi  ← 回退源需要"

    import os
    mx_status = "已配置" if os.environ.get("MX_APIKEY") else "未配置 → export MX_APIKEY=your_key  ← 妙想API必需"
    if HAS_MOOMOO:
        _st = mm.status()
        mm_status = (f"已连接 {_st['host']}:{_st['port']} (SDK={_st['sdk']})" if _st["ok"]
                     else f"不可用 → {_st['error']}")
    else:
        mm_status = "未安装 → pip install moomoo-api"

    print(f"""
╔══════════════════════════════════════════════════════╗
║   A股风险监测 本地代理服务器  v4 (moomoo)             ║
║   监听端口  : {PORT}                                   ║
║   moomoo    : {mm_status}
║   requests  : {req_status}
║   curl_cffi : {cffi_status}
║   MX_APIKEY : {mx_status}
║                                                      ║
║   GET  /index_kline?code=sh000300&days=60            ║
║        → moomoo 日K（真实成交额），回退新浪          ║
║   GET  /quote?code=sh000300,510300 → moomoo 快照     ║
║   GET  /health    → 各数据源状态自检                  ║
║   GET  /prebuilt /pe /bond /sectors /margin /sf      ║
║        /fund /etf_categories /turnover → 每日 JSON   ║
║   GET  /?url=...  → 通用反代（CORS/TLS绕过，回退用） ║
║   POST /mx        → 东财妙想API代理                   ║
╚══════════════════════════════════════════════════════╝
""", flush=True)

    if HAS_MOOMOO and not mm.status()["ok"]:
        print("[Proxy] ⚠  moomoo OpenD 未连接：盘中行情将回退新浪/东财，"
              "请先启动 OpenD 并登录（默认 127.0.0.1:11111）\n", flush=True)
    if not HAS_CURL_CFFI:
        print("[Proxy] ⚠  未装 curl_cffi：moomoo 不可用时的东财/新浪回退会 502\n", flush=True)

    server = HTTPServer(("localhost", PORT), ProxyHandler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        if HAS_MOOMOO:
            mm.close()
        print("\n[Proxy] 已停止", flush=True)
        sys.exit(0)
