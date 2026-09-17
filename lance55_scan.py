#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
55 战法扫盘脚本（兰斯洛t 55线介入战法 / 唯一引擎）

逻辑（日线级别，全市场沪深）：
  1) 前提：5/10/20 均线多头排列
  2) 事件：最近 1-3 日内，放量大阳（涨幅>=X%，量>=5日均量*Y）首次上穿日 55 线
  3) 买点：突破后 1-3 日内，盘中击穿/触及 55 线（low<=ma55*buf）
           —— 若收盘缩量落在 55 线下（B类），或收盘收回 55 线上（A类当天回抽，最强）
  4) 止损：突破日最低 与 ma55*(1-k) 取更低者；定量止损优先于技术

输出：data.json + data.js（file:// 看板用 js，避免 CORS）
用法：
  python lance55_scan.py --limit 200      # 小样本冒烟（不写缓存）
  python lance55_scan.py                  # 全量（写缓存）
  python lance55_scan.py --refresh        # 忽略缓存强制重拉
"""
import json
import os
import sys
import time
import gzip
import threading
import urllib.request
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

BASE = os.path.dirname(os.path.abspath(__file__))
CACHE = os.path.join(BASE, "cache")
CONFIG_PATH = os.path.join(BASE, "lance55_config.json")
os.makedirs(CACHE, exist_ok=True)

UA = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"),
    "Referer": "https://gu.qq.com/",
    "Accept": "*/*",
}

LOGS = []


def log(msg):
    line = "[%s] %s" % (datetime.now().strftime("%H:%M:%S"), msg)
    LOGS.append(line)
    print(line, flush=True)


SINA_UA = dict(UA, Referer="https://finance.sina.com.cn")


def http_get(url, enc="utf-8", timeout=20, retry=2, headers=None):
    last = None
    for _ in range(retry + 1):
        try:
            req = urllib.request.Request(url, headers=headers or UA)
            r = urllib.request.urlopen(req, timeout=timeout)
            data = r.read()
            if r.headers.get("Content-Encoding") == "gzip":
                data = gzip.decompress(data)
            return data.decode(enc, "ignore")
        except Exception as e:
            last = e
            time.sleep(0.6)
    raise last


# ============================================================
# 1) 全 A 列表（新浪，沪深，剔除北交所）
# ============================================================
def fetch_universe(limit=None):
    path = os.path.join(CACHE, "universe.json")
    trade_date = None
    out = []
    page = 1
    empty_streak = 0
    # 注意：新浪该接口 num 上限是 100（传 500 也只回 100），且偶发返回空页，
    # 连续 2 页空才认为到底，否则会像早期版本一样只拿到 3656 只（漏 1500+）
    while page <= 80:
        url = ("https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/"
               "Market_Center.getHQNodeData?page=%d&num=100&sort=symbol&asc=1&node=hs_a&symbol=&_s_r_a=page" % page)
        try:
            txt = http_get(url, headers=SINA_UA).strip()
        except Exception:
            empty_streak += 1
            if empty_streak >= 2:
                break
            page += 1
            continue
        if not txt or txt in ("null", "[]"):
            empty_streak += 1
            if empty_streak >= 2:
                break
            page += 1
            continue
        try:
            arr = json.loads(txt)
        except Exception:
            empty_streak += 1
            if empty_streak >= 2:
                break
            page += 1
            continue
        if not arr:
            empty_streak += 1
            if empty_streak >= 2:
                break
            page += 1
            continue
        empty_streak = 0
        for it in arr:
            sym = it.get("symbol", "")
            if sym.startswith("bj"):
                continue
            out.append({
                "symbol": sym,
                "code": it.get("code", ""),
                "name": it.get("name", ""),
                "price": float(it.get("trade") or 0),
                "chg": float(it.get("changepercent") or 0),
                "amount": float(it.get("amount") or 0),
                "mktcap": float(it.get("mktcap") or 0),
                "turnover": float(it.get("turnoverratio") or 0),
                "pe": it.get("per") or "",
                "pb": it.get("pb") or "",
            })
            tk = it.get("ticktime") or ""
            if not trade_date and len(tk) >= 15:
                trade_date = tk  # 形如 "15:30:01"，日期需另取
        page += 1
        if limit and len(out) >= limit:
            break
    # 真实交易日：从实时行情取日期字段（必须带 sina Referer，否则 403）
    try:
        q = http_get("https://hq.sinajs.cn/list=sh000001", enc="gbk", headers=SINA_UA)
        p = q.split('"')[1].split(",")
        trade_date = p[30] if len(p) > 31 else None
    except Exception:
        pass
    if limit:
        out = out[:limit]
    log("  全A列表: %d 只（沪深，已剔北交所），交易日=%s" % (len(out), trade_date))
    return out, trade_date


# ============================================================
# 2) 行业映射（新浪板块 -> 成分股），失败可降级
# ============================================================
def fetch_industry(force=False):
    path = os.path.join(CACHE, "industry.json")
    if os.path.exists(path) and force:
        os.remove(path)
    if os.path.exists(path):
        age = time.time() - os.path.getmtime(path)
        if age < 7 * 86400:
            try:
                with open(path, "r", encoding="utf-8") as f:
                    c = json.load(f)
                # 必须返回 tuple：直接 return json.load 会解包成键名字符串，
                # 后续 ind_of.get() 抛异常 -> 被上层静默吞掉 -> 全市场 0 命中（踩过）
                return c.get("ind_of", {}), c.get("perf", {})
            except Exception:
                pass
    try:
        txt = http_get("https://vip.stock.finance.sina.com.cn/q/view/newSinaHy.php", enc="gbk", headers=SINA_UA)
        import re
        m = re.search(r"=\s*(\{.*\})", txt, re.S)
        hy = json.loads(m.group(1))
    except Exception as e:
        log("  行业列表获取失败(%s) -> 降级为无行业" % str(e)[:30])
        return {}, {}
    code2name = {}
    for k, v in hy.items():
        p = v.split(",")
        if len(p) > 1:
            code2name[p[0]] = p[1]
    ind_of = {}
    perf = {}
    # 注意 num 上限 100，必须翻页，否则每个板块只映射到 100 只（覆盖会腰斩）
    for code, nm in code2name.items():
        for page in range(1, 31):
            try:
                url = ("https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/"
                       "Market_Center.getHQNodeData?page=%d&num=100&sort=symbol&asc=1&node=%s"
                       "&symbol=&_s_r_a=page" % (page, urllib.parse.quote(code)))
                arr = json.loads(http_get(url, headers=SINA_UA, retry=1) or "[]")
            except Exception:
                break
            if not arr:
                break
            for it in arr:
                ind_of[it.get("code", "")] = nm
            if len(arr) < 100:
                break
            time.sleep(0.05)
    # 板块涨跌幅
    try:
        txt2 = http_get("https://vip.stock.finance.sina.com.cn/q/view/newSinaHy.php", enc="gbk", headers=SINA_UA)
        for k, v in hy.items():
            p = v.split(",")
            if len(p) > 5:
                try:
                    perf[p[1]] = float(p[5])
                except Exception:
                    pass
    except Exception:
        pass
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"ind_of": ind_of, "perf": perf}, f, ensure_ascii=False)
    log("  行业映射: %d 只成分股, %d 个板块" % (len(ind_of), len(perf)))
    return ind_of, perf


# ============================================================
# 3) 日线（腾讯前复权，含当日），带缓存 + 覆盖度检查
# ============================================================
def load_kline_cache(trade_date):
    path = os.path.join(CACHE, "klines_tx.json")
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            c = json.load(f)
        if c.get("date") != trade_date:
            return None
        return c.get("data", {})
    except Exception:
        return None


def save_kline_cache(trade_date, data):
    path = os.path.join(CACHE, "klines_tx.json")
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"date": trade_date, "data": data}, f)
    if os.path.exists(path):
        os.remove(path)
    os.rename(tmp, path)


class RateLimiter:
    """全局限速：免费行情接口靠限速而不是更高的并发来保命。
    实测教训：6 并发连打 300 只（~50 req/s）后腾讯 WAF 直接 501 封 IP。
    默认 12 req/s，安全且 5200 只约 7 分钟（当天只跑一次，有缓存）。"""

    def __init__(self, rate):
        self.min_interval = (1.0 / rate) if rate and rate > 0 else 0.0
        self.lock = threading.Lock()
        self.next_t = 0.0

    def wait(self):
        if self.min_interval <= 0:
            return
        with self.lock:
            now = time.time()
            if now < self.next_t:
                time.sleep(self.next_t - now)
                now = time.time()
            self.next_t = now + self.min_interval


# 主源 proxy.finance.qq.com（与 web.ifzq 同数据，但另一条边缘，WAF 各自独立）
# 备源 web.ifzq.gtimg.cn（主源被封时用）
KLINE_HOSTS = [
    "https://proxy.finance.qq.com/ifzqgtimg/appstock/app/newfqkline/get?param=%s,day,,,%d,qfq",
    "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param=%s,day,,,%d,qfq",
]

_LIMITER = None
_FUSE = {"fail": 0, "tripped": False}
_FUSE_LOCK = threading.Lock()


def fetch_one(sym, datalen):
    global _LIMITER
    if _LIMITER is not None:
        _LIMITER.wait()
    last_err = None
    for host in KLINE_HOSTS:
        try:
            j = json.loads(http_get(host % (sym, datalen), timeout=15, retry=0))
            d = j.get("data", {}).get(sym, {})
            k = d.get("qfqday") or d.get("day") or []
            # 腾讯格式: [date, open, close, high, low, volume, ...]
            bars = []
            for row in k:
                try:
                    bars.append([row[0], float(row[1]), float(row[2]), float(row[3]),
                                 float(row[4]), float(row[5])])
                except Exception:
                    continue
            if bars:
                with _FUSE_LOCK:
                    _FUSE["fail"] = 0
                return sym, bars
        except Exception as e:
            last_err = e
            continue
    with _FUSE_LOCK:
        _FUSE["fail"] += 1
    return sym, []


def fuse_tripped(limit):
    with _FUSE_LOCK:
        return _FUSE["fail"] >= limit


def fetch_klines(symbols, datalen, workers, rate, cache, use_cache=True):
    global _LIMITER
    _LIMITER = RateLimiter(rate)
    todo = [s for s in symbols if (not use_cache) or s not in cache]
    log("  日线: 需拉取 %d 只（缓存命中 %d 只），并发 %d，限速 %.0f req/s"
        % (len(todo), len(symbols) - len(todo), workers, rate))
    if not todo:
        return cache
    done = 0
    fail = 0
    t0 = time.time()
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(fetch_one, s, datalen): s for s in todo}
        for fu in as_completed(futs):
            s = futs[fu]
            try:
                sym, bars = fu.result()
                if bars:
                    cache[sym] = bars
                    done += 1
                else:
                    fail += 1
            except Exception:
                fail += 1
            if (done + fail) % 500 == 0:
                log("    进度 %d/%d  成功 %d 失败 %d  (%.1f req/s)"
                    % (done + fail, len(todo), done, fail, (done + fail) / (time.time() - t0 + 1e-9)))
            if fuse_tripped(40) and done + fail < len(todo):
                log("  !! 熔断：连续失败 %d 次（源大概率被限流/封 IP）-> 停止拉取" % 40)
                for f in futs:
                    f.cancel()
                break
    log("  日线完成: 成功 %d, 失败 %d, 用时 %.1fs" % (done, fail, time.time() - t0))
    return cache


# ============================================================
# 4) 指标
# ============================================================
def mean(xs):
    return sum(xs) / float(len(xs)) if xs else 0.0


def ma_series(arr, n):
    """前缀和滚动均值；返回 list，series[i] 对应 arr[i+n-1]"""
    if len(arr) < n:
        return None
    cs = [0.0] * (len(arr) + 1)
    for i, v in enumerate(arr):
        cs[i + 1] = cs[i] + v
    return [(cs[i + n] - cs[i]) / float(n) for i in range(len(arr) - n + 1)]


# ============================================================
# 5) 单只扫描
# ============================================================
def scan_one(meta, bars, cfg, ind_of, perf):
    """返回 None 或 dict"""
    u, t, b, p, r = cfg["universe"], cfg["trend"], cfg["breakout"], cfg["pullback"], cfg["risk"]
    code = meta["code"]
    name = meta["name"]
    for kw in u["exclude_name_keywords"]:
        if kw in name.upper():
            return None
    if len(bars) < max(u["min_bars"], t["signal_ma"] + 5):
        return None
    if meta["price"] <= 0 or meta["price"] > u["max_price"]:
        return None
    if meta["amount"] < u["min_amount_yi"] * 1e8:
        return None

    dates = [x[0] for x in bars]
    close = [x[2] for x in bars]
    openp = [x[1] for x in bars]
    high = [x[3] for x in bars]
    low = [x[4] for x in bars]
    vol = [x[5] for x in bars]

    n = len(close)
    m5, m10, m20, m55 = t["bull_ma"] + [t["signal_ma"]]
    s5, s10, s20, s55 = ma_series(close, m5), ma_series(close, m10), ma_series(close, m20), ma_series(close, m55)
    if s55 is None or s20 is None:
        return None
    # series 对齐：series[i] 对应 bars[m-1+i]
    def sv(series, idx, m):
        j = idx - (m - 1)
        return float(series[j]) if 0 <= j < len(series) else None

    last = n - 1
    # --- 多头排列（当前日，且突破日也要满足）---
    def bull_at(i):
        a, bb, c = sv(s5, i, m5), sv(s10, i, m10), sv(s20, i, m20)
        if a is None or bb is None or c is None:
            return False
        return a > bb > c

    if not bull_at(last):
        return None

    # --- 找突破日（最近 lookback 天内，取最晚一次）---
    lb = b["lookback_days"]
    brk = None
    for i in range(max(1, last - lb), last + 1):
        c55_prev, c55 = sv(s55, i - 1, m55), sv(s55, i, m55)
        if c55_prev is None or c55 is None:
            continue
        if close[i - 1] > c55_prev or close[i] <= c55:
            continue
        gain = (close[i] / close[i - 1] - 1) * 100
        vma = mean(vol[max(0, i - t["vol_ma"]):i])
        vr = (vol[i] / vma) if vma > 0 else 0.0
        if gain < b["min_gain_pct"] or vr < b["min_vol_ratio"]:
            continue
        if b["require_yang"] and close[i] <= openp[i]:
            continue
        if not bull_at(i):
            continue
        brk = {"idx": i, "date": dates[i], "gain": round(gain, 2), "vol_ratio": round(vr, 2)}
    if brk is None:
        return None

    bi = brk["idx"]
    ma55_now = sv(s55, last, m55)
    vma_now = mean(vol[max(0, last - t["vol_ma"]):last])
    vr_now = (vol[last] / vma_now) if vma_now > 0 else 0.0

    # --- 回踩判定：突破后 1..max_days_after 天，取最晚一次命中 ---
    hits = []
    for d in range(bi + 1, min(last, bi + p["max_days_after"]) + 1):
        c55 = sv(s55, d, m55)
        if c55 is None:
            continue
        if low[d] > c55 * (1 + p["touch_buf"]):
            continue
        below_pct = (close[d] / c55 - 1) * 100
        if below_pct < -p["max_close_below_pct"]:
            continue
        vma = mean(vol[max(0, d - t["vol_ma"]):d])
        vr = (vol[d] / vma) if vma > 0 else 0.0
        kind = "A-盘中击穿" if close[d] >= c55 else "B-缩量落线下"
        if close[d] < c55 and vr > p["shrink_vol_ratio"]:
            kind = "C-放量破位(慎)"
        hits.append({
            "idx": d, "date": dates[d], "kind": kind,
            "vol_ratio": round(vr, 2),
            "close_vs_ma55": round(below_pct, 2),
            "is_today": d == last,
            "reclaim": bool(close[d] >= c55 and low[d] < c55),
        })
    hit = hits[-1] if hits else None

    if hit is None:
        stage = "WATCH"   # 待回踩
        dist = (close[last] / ma55_now - 1) * 100
    else:
        stage = "BUY_TODAY" if hit["is_today"] else "BUY_RECENT"
        dist = hit["close_vs_ma55"]

    # --- 附加信息 ---
    chg20 = (close[last] / close[last - 20] - 1) * 100 if last >= 20 else 0.0
    if chg20 > r["max_chg20_pct"]:
        return None
    stop_tech = min(low[bi], ma55_now * (1 - r["stop_below_ma55_pct"] / 100))
    stop_pct = (stop_tech / close[last] - 1) * 100
    hard_stop = close[last] * (1 - r["hard_stop_pct"] / 100)
    s233 = ma_series(close, t["target_ma"])
    target233 = float(s233[-1]) if s233 else None
    recent_high = max(high[max(0, last - 60):last + 1])
    industry = ind_of.get(code, "")
    ind_perf = perf.get(industry)

    # --- 评分 ---
    score = 0.0
    a5, a10, a20 = sv(s5, last, m5), sv(s10, last, m10), sv(s20, last, m20)
    if a5 > a10 > a20:
        score += 25
        if s20 is not None and len(s20) > 5 and s20[-1] > s20[-6]:
            score += 5
    vrb = brk["vol_ratio"]
    score += 25 if vrb >= 3 else 18 if vrb >= 2 else 12
    if hit:
        if hit["vol_ratio"] <= 0.6:
            score += 20
        elif hit["vol_ratio"] <= 0.8:
            score += 14
        elif hit["vol_ratio"] <= 1.0:
            score += 8
    if stage == "BUY_TODAY":
        score += 8
    if hit and hit["reclaim"]:
        score += 10
    ad = abs(dist)
    score += 15 if ad <= 1 else 10 if ad <= 3 else 5 if ad <= 5 else 0
    score += 5 if chg20 < 30 else (0 if chg20 < 60 else -10)
    if ind_perf is not None:
        score += 6 if ind_perf > 1 else 3 if ind_perf > 0 else -3

    # 当日形态补充（诚实标注：触及 与 只是接近 要能区分）
    amp = (high[last] - low[last]) / low[last] * 100 if low[last] > 0 else 0.0
    low_vs55 = (low[last] / ma55_now - 1) * 100
    one_word = abs(openp[last] - high[last]) < 1e-6 and abs(high[last] - low[last]) < 1e-6
    lim = 19.5 if code.startswith(("30", "68")) else 9.8
    limit_up = meta["chg"] >= lim
    limit_down = meta["chg"] <= -lim

    return {
        "code": code, "name": name, "symbol": meta["symbol"],
        "amplitude": round(float(amp), 1),
        "low_vs_ma55_pct": round(float(low_vs55), 2),
        "one_word": bool(one_word), "limit_up": bool(limit_up), "limit_down": bool(limit_down),
        "industry": industry, "ind_perf": ind_perf,
        "price": round(float(close[last]), 3),
        "chg": round(meta["chg"], 2),
        "amount_yi": round(meta["amount"] / 1e8, 2),
        "mktcap_yi": round(meta["mktcap"] / 1e4, 1),
        "turnover": round(meta["turnover"], 2),
        "stage": stage,
        "hit_kind": hit["kind"] if hit else "",
        "hit_date": hit["date"] if hit else "",
        "hit_vol_ratio": hit["vol_ratio"] if hit else None,
        "reclaim": hit["reclaim"] if hit else False,
        "break_date": brk["date"],
        "break_gain": brk["gain"],
        "break_vol_ratio": brk["vol_ratio"],
        "days_after": last - bi,
        "ma5": round(a5, 3), "ma10": round(a10, 3), "ma20": round(a20, 3),
        "ma55": round(ma55_now, 3),
        "dist_ma55_pct": round(float(dist), 2),
        "vol_ratio_today": round(vr_now, 2),
        "chg20": round(float(chg20), 2),
        "stop_tech": round(float(stop_tech), 3),
        "stop_tech_pct": round(float(stop_pct), 2),
        "stop_hard": round(float(hard_stop), 3),
        "target_233": round(target233, 3) if target233 else None,
        "recent_high": round(recent_high, 3),
        "score": round(float(score), 1),
        "last_date": dates[last],
    }


# ============================================================
# 6) 大盘环境（上证）
# ============================================================
def market_env(cfg):
    try:
        _, bars = fetch_one("sh000001", 320)
        close = [x[2] for x in bars]
        s5, s10, s20, s55 = (ma_series(close, 5), ma_series(close, 10),
                             ma_series(close, 20), ma_series(close, 55))
        last = len(close) - 1
        def sv(series, m):
            return float(series[last - (m - 1)])
        a5, a10, a20, a55 = sv(s5, 5), sv(s10, 10), sv(s20, 20), sv(s55, 55)
        chg = (close[last] / close[last - 1] - 1) * 100
        bull = a5 > a10 > a20
        above55 = close[last] > a55
        if bull and above55:
            tag, desc = "多头强势", "指数 5>10>20 且站上 55 线：趋势战法环境最好"
        elif above55:
            tag, desc = "震荡偏强", "站上 55 线但均线未完全多头排列：可参与，仓位收敛"
        elif bull:
            tag, desc = "修复中", "短期均线多头但仍在 55 线下方：反弹属性，买点需更严格"
        else:
            tag, desc = "弱势", "空头/55 线下：他明说『大盘不企稳开仓赚不到钱』，建议只观察"
        return {
            "date": bars[-1][0], "close": round(float(close[last]), 2),
            "chg": round(float(chg), 2), "ma5": round(a5, 2), "ma10": round(a10, 2),
            "ma20": round(a20, 2), "ma55": round(a55, 2),
            "tag": tag, "desc": desc,
        }
    except Exception as e:
        return {"tag": "未知", "desc": "指数数据获取失败: %s" % str(e)[:40]}


# ============================================================
# main
# ============================================================
def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0, help="小样本只数，>0 时不写缓存")
    ap.add_argument("--refresh", action="store_true", help="强制重拉日线")
    ap.add_argument("--no-industry", action="store_true")
    ap.add_argument("--refresh-industry", action="store_true", help="强制重拉行业映射")
    args = ap.parse_args()

    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        cfg = json.load(f)

    log("=== 55战法扫盘 开始 ===")
    uni, trade_date = fetch_universe()
    if not trade_date:
        trade_date = datetime.now().strftime("%Y-%m-%d")
        log("  交易日取实时失败，回退为系统日期 %s" % trade_date)

    ind_of, perf = ({}, {})
    if cfg["scan"]["industry"] and not args.no_industry:
        try:
            ind_of, perf = fetch_industry(force=args.refresh_industry)
        except Exception as e:
            log("  行业映射异常，降级: %s" % str(e)[:40])

    symbols = [u["symbol"] for u in uni]
    datalen = cfg["scan"]["datalen"]
    cache = {} if args.limit else (load_kline_cache(trade_date) or {})
    if args.refresh:
        cache = {}
    if not args.limit and cache:
        cover = len([s for s in symbols if s in cache]) / max(1, len(symbols))
        log("  缓存命中率 %.1f%%（%d/%d），阈值 80%%" % (cover * 100, len([s for s in symbols if s in cache]), len(symbols)))
        if cover < 0.8:
            log("  覆盖率不足 -> 全量重拉")
            cache = {}
    if args.limit:
        symbols = symbols[:args.limit]

    cache = fetch_klines(symbols, datalen, cfg["scan"]["workers"], cfg["scan"]["rate"],
                         cache, use_cache=not args.refresh)
    if not args.limit:
        save_kline_cache(trade_date, cache)
        log("  缓存已写入 cache/klines_tx.json (%d 只)" % len(cache))

    env = market_env(cfg)
    log("  大盘环境: 上证 %s %s (%.2f, %+.2f%%) 55线=%.2f" %
        (env.get("date", ""), env.get("tag", ""), env.get("close", 0),
         env.get("chg", 0), env.get("ma55", 0)))

    # ---- 扫描 ----
    t0 = time.time()
    res = []
    n_valid = 0
    n_break = 0
    n_err = 0
    err_samples = []
    for u in uni:
        bars = cache.get(u["symbol"])
        if not bars:
            continue
        n_valid += 1
        try:
            r = scan_one(u, bars, cfg, ind_of, perf)
        except Exception as e:
            n_err += 1
            if len(err_samples) < 3:
                err_samples.append("%s: %s" % (u["symbol"], str(e)[:60]))
            continue
        if r:
            res.append(r)
            n_break += 1
    log("  扫描: 有效K线 %d 只 -> 命中(多头+突破55) %d 只，异常 %d 只，用时 %.1fs" %
        (n_valid, n_break, n_err, time.time() - t0))
    for s in err_samples:
        log("    !! 异常样本 %s" % s)

    buy_today = [r for r in res if r["stage"] == "BUY_TODAY"]
    buy_recent = [r for r in res if r["stage"] == "BUY_RECENT"]
    watch = [r for r in res if r["stage"] == "WATCH"]
    for lst in (buy_today, buy_recent, watch):
        lst.sort(key=lambda x: -x["score"])

    log("  分级: 今日买点 %d / 窗口内已触发 %d / 待回踩观察 %d" %
        (len(buy_today), len(buy_recent), len(watch)))
    for r in buy_today[:10]:
        log("    🔥 %s %s  %s  距55线%+.2f%% 回踩量比%.2f 分%.1f" %
            (r["code"], r["name"], r["hit_kind"], r["dist_ma55_pct"],
             r["hit_vol_ratio"] or 0, r["score"]))

    out = {
        "meta": {
            "generated": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "trade_date": trade_date,
            "universe": len(uni),
            "scanned": n_valid,
            "industry_cover": len(ind_of),
            "config": cfg,
            "market": env,
            "logs": LOGS[-40:],
        },
        "buy_today": buy_today[:cfg["scan"]["top_n"]],
        "buy_recent": buy_recent[:cfg["scan"]["top_n"]],
        "watch": watch[:cfg["scan"]["top_n"]],
        "stats": {
            "buy_today": len(buy_today),
            "buy_recent": len(buy_recent),
            "watch": len(watch),
            "scanned": n_valid,
        },
    }
    with open(os.path.join(BASE, "data.json"), "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    with open(os.path.join(BASE, "data.js"), "w", encoding="utf-8") as f:
        f.write("window.__DATA__=")
        json.dump(out, f, ensure_ascii=False)
        f.write(";")
    log("  输出: data.json + data.js")

    # 行业分布（命中的今日买点 + 观察池）
    from collections import Counter
    cnt = Counter([r["industry"] or "未分类" for r in (buy_today + watch)])
    log("  命中行业 TOP8: " + ", ".join("%s:%d" % (k, v) for k, v in cnt.most_common(8)))
    log("=== 完成 ===")


if __name__ == "__main__":
    main()
