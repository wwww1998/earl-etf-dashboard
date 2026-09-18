# -*- coding: utf-8 -*-
"""
EarlETF 图表周刊 · 指标自动计算引擎
多源抓取 -> 本地缓存(断点续传) -> 计算 11 个模块 -> 输出 data.json / data.js
"""
import os, io, sys, json, time, math, bisect, warnings, datetime as dt, tempfile
import requests
import numpy as np
import pandas as pd

warnings.filterwarnings("ignore")

BASE = os.path.dirname(os.path.abspath(__file__))


def _atomic_write(path, text):
    """先写临时文件再原子替换，避免前端轮询读到半截 JSON。"""
    d = os.path.dirname(path) or "."
    fd, tmp = tempfile.mkstemp(dir=d, suffix=".atmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, path)
    except Exception:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


CACHE = os.path.join(BASE, "cache")
RAW = os.path.join(CACHE, "tc_raw")          # 成交集中度：个股成交额原始落盘
os.makedirs(CACHE, exist_ok=True)
SINA_K = "https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/CN_MarketData.getKLineData"
TODAY = dt.date.today()
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36")

DISPLAY_FROM = "2008-01-01"   # 图表展示起点（计算仍用全历史）
# M1「五年之锚」用万得全A(1999 起)，五年均线自 2004-12 起有值 → 单独把展示窗口提前，
# 让 10 年偏离度百分位曲线（自 2006-01 起）完整可见。
M1_DISPLAY_FROM = "2004-12-31"
# M1b 原文口径版：同样用五年均线（自 2004-12 起有值），但把价格窗口再往前拨到
# 2004 年初，让用户看到 2004-05 年的行情背景（早期只有万得全A价格线，均线/抄底线待热身）。
M1B_DISPLAY_FROM = "2004-01-01"
SLEEP = 3.5          # 中证官网请求间隔(秒)，避免 WAF 限流（GH Actions 跑冷启动时 2.6s 仍会触发）
BACKFILL_FROM = 2005
CHUNK_YEARS = 6      # 单次请求跨度（中证官网实测支持 6 年）

# 行情：key -> (显示名, [(源类型, 代码, 是否同一指数可合并), ...])
# 顺序 = 优先级。无替代源的代码排在前面，优先抢到限流窗口。
SERIES = {
    "930950": ("中证偏股基金",  [("csi", "930950", True)]),
    "H11023": ("中证债券基金",  [("csi", "H11023", True)]),
    "931591": ("1000成长创新",  [("csi", "931591", True)]),
    "931588": ("1000价值稳健",  [("csi", "931588", True)]),
    "930914": ("港股通高股息",  [("csi", "930914", True)]),
    # 官方全收益指数（H 前缀 / CNY01 后缀，中证官网发布）——原文口径即全收益
    "H00300": ("沪深300全收益", [("csi", "H00300", True), ("csi", "000300", False)]),
    "H00922": ("中证红利全收益", [("csi", "H00922", True), ("csi", "000922", False)]),
    "H31589": ("300成长创新全收益", [("csi", "931589CNY01", False)]),
    "H31586": ("300价值稳健全收益", [("csi", "931586", True), ("csi", "931586CNY01", True)]),
    "H31591": ("1000成长创新全收益", [("csi", "931591CNY01", False), ("csi", "931591", False)]),
    "H31588": ("1000价值稳健全收益", [("csi", "931588CNY01", False), ("csi", "931588", False)]),
    "931589": ("300成长创新",   [("csi", "931589", True), ("tx", "sh000918", False)]),
    "931586": ("300价值稳健",   [("csi", "931586", True), ("tx", "sh000919", False)]),
    "930903": ("中证A股",       [("csi", "930903", True), ("tx", "sh000985", False)]),
    "000300": ("沪深300",       [("csi", "000300", True), ("tx", "sh000300", True)]),
    "000922": ("中证红利",      [("csi", "000922", True), ("tx", "sh000922", True)]),
    "000852": ("中证1000",      [("csi", "000852", True), ("tx", "sh000852", True)]),
    # 上证红利由中证指数公司编制 → 改走中证官网（官方），腾讯降级为兜底
    "000015": ("上证红利",      [("csi", "000015", True), ("tx", "sh000015", True)]),
    # 国证指数 → 国证指数网官方行情接口（hq.cnindex.com.cn），单次可拿 2005 年至今全历史
    "sz399370": ("国证成长",    [("cni", "399370", True), ("tx", "sz399370", True)]),
    "sz399371": ("国证价值",    [("cni", "399371", True), ("tx", "sz399371", True)]),
}

# 降级说明（当主源不可用时展示）
FALLBACK_NOTE = {
    "930903": ("中证A股 → 中证全指(000985)",
               "中证A股(930903) 无公开日频源时改用中证全指(000985)，两者同为全市场综合指数，走势高度一致。"),
    "H31589": ("300成长创新全收益 → 价格指数931589",
               "全收益版本(931589CNY01)无公开日频源时，降级回退价格指数931589，风格含义一致。"),
    "H31586": ("300价值稳健全收益 → 价格指数931586",
               "全收益版本(931586CNY01)无公开日频源时，降级回退价格指数931586，风格含义一致。"),
    "H31591": ("1000成长创新全收益 → 价格指数931591",
               "全收益版本(931591CNY01)无公开日频源时，降级回退价格指数931591，风格含义一致。"),
    "H31588": ("1000价值稳健全收益 → 价格指数931588",
               "全收益版本(931588CNY01)无公开日频源时，降级回退价格指数931588，风格含义一致。"),
    "931589": ("300成长创新 → 沪深300成长(000918)",
               "中证智选300成长创新(931589) 无公开日频源时改用沪深300成长(000918)，风格含义一致。"),
    "931586": ("300价值稳健 → 沪深300价值(000919)",
               "中证智选300价值稳健(931586) 无公开日频源时改用沪深300价值(000919)，风格含义一致。"),
}


def log(m):
    # stdout 被重定向到文件时 Python 用 locale 编码（Windows 常为 GBK），
    # 遇到 ⬆/⬇ 等字符会 UnicodeEncodeError，这里强制 UTF-8 并兜底替换。
    try:
        print(f"[{dt.datetime.now():%H:%M:%S}] {m}", flush=True)
    except UnicodeEncodeError:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        print(f"[{dt.datetime.now():%H:%M:%S}] {m}", flush=True)


def cpath(key):
    return os.path.join(CACHE, f"{key.replace('/','_')}.csv")


def load(key):
    p = cpath(key)
    if not os.path.exists(p):
        return None
    try:
        df = pd.read_csv(p)
        return df if not df.empty else None
    except Exception:
        return None


def save(key, df):
    try:
        df.to_csv(cpath(key), index=False)
    except Exception as e:
        log(f"  缓存失败 {key}: {e}")


def merge(old, new, on="date"):
    if old is None or len(old) == 0:
        return new.reset_index(drop=True) if new is not None else None
    if new is None or len(new) == 0:
        return old
    df = pd.concat([old, new], ignore_index=True)
    df[on] = df[on].astype(str)
    return df.drop_duplicates(subset=[on], keep="last").sort_values(on).reset_index(drop=True)


# ----------------------------------------------------------- 中证官网 API
_csi = None


def csi_sess(force=False):
    global _csi
    if _csi is None or force:
        s = requests.Session()
        s.headers.update({"User-Agent": UA, "Accept": "text/html,*/*",
                          "Accept-Language": "zh-CN,zh;q=0.9"})
        try:
            s.get("https://www.csindex.com.cn/", timeout=25)
        except Exception:
            pass
        s.headers.update({"Referer": "https://www.csindex.com.cn/#/indices/family/detail",
                          "Accept": "application/json, text/plain, */*",
                          "X-Requested-With": "XMLHttpRequest"})
        _csi = s
    return _csi


CSI_BLOCKED = [False, 0]     # [是否已判定本次运行被封, 连续403次数]
# 中证官网 403 是 IP 级临时限流：同一秒的另一个指数请求照样 403，逐个重试纯属浪费。
# 实测（2026-09-17）连续 4 次 403 各退避 50s = 白等 205s，占整轮 439s 的 47%。
# ⇒ 阈值降到 2（只给一次短期重试机会），并把限流状态落盘做冷却，避免同一小时内多轮重复撞墙。
CSI_BLOCKED_THRESHOLD = 2
CSI_WAF_SLEEP = 15           # 403 后的重试退避秒数（原 50s）
CSI_WAF_COOLDOWN_MIN = float(os.environ.get("CSI_WAF_COOLDOWN_MIN", "12"))
CSI_WAF_FLAG = os.path.join(CACHE, "_csi_waf_cooldown.txt")


def _csi_waf_active():
    """上轮被限流后的冷却期内 → 本轮不再发任何中证请求。"""
    if CSI_WAF_COOLDOWN_MIN <= 0:
        return False
    try:
        if os.path.exists(CSI_WAF_FLAG):
            until = float((open(CSI_WAF_FLAG, encoding="utf-8").read() or "0").strip())
            return time.time() < until
    except Exception:
        pass
    return False


def _csi_waf_trip():
    """落盘限流冷却（跨进程有效）。"""
    try:
        os.makedirs(CACHE, exist_ok=True)
        with open(CSI_WAF_FLAG, "w", encoding="utf-8") as fh:
            fh.write(str(time.time() + CSI_WAF_COOLDOWN_MIN * 60))
    except Exception:
        pass


def _csi_waf_clear():
    try:
        if os.path.exists(CSI_WAF_FLAG):
            os.remove(CSI_WAF_FLAG)
    except Exception:
        pass


def csindex_api(code, start, end, tries=3):
    if CSI_BLOCKED[0]:
        return pd.DataFrame(columns=["date", "close"])
    if _csi_waf_active():
        CSI_BLOCKED[0] = True
        log(f"    中证官网限流冷却期内（{CSI_WAF_COOLDOWN_MIN:.0f} min），本轮跳过中证源（不消耗请求）")
        return pd.DataFrame(columns=["date", "close"])
    s = csi_sess()
    for i in range(tries):
        try:
            if CSI_BLOCKED[0]:
                return pd.DataFrame(columns=["date", "close"])
            r = s.get("https://www.csindex.com.cn/csindex-home/perf/index-perf",
                      params={"indexCode": code, "startDate": start, "endDate": end}, timeout=45)
            if r.status_code == 403:
                CSI_BLOCKED[1] += 1
                if CSI_BLOCKED[1] >= CSI_BLOCKED_THRESHOLD:
                    CSI_BLOCKED[0] = True
                    _csi_waf_trip()
                    log(f"    中证官网限流（连续 {CSI_BLOCKED[1]} 次 403）→ 跳过中证源，冷却 {CSI_WAF_COOLDOWN_MIN:.0f} min")
                    return pd.DataFrame(columns=["date", "close"])
                log(f"    {code} {start}~{end} WAF(403)，退避 {CSI_WAF_SLEEP}s 后重试 1 次")
                time.sleep(CSI_WAF_SLEEP)
                csi_sess(force=True)
                continue
            CSI_BLOCKED[1] = 0
            _csi_waf_clear()
            if r.status_code != 200:
                time.sleep(6)
                continue
            rows = (r.json() or {}).get("data") or []
            if not rows:
                return pd.DataFrame(columns=["date", "close", "pe"])
            rec = []
            for x in rows:
                d = c = None
                if isinstance(x, dict):
                    d = x.get("tradeDate") or x.get("date")
                    c = x.get("close")
                elif isinstance(x, (list, tuple)) and len(x) > 9:
                    d, c = x[0], x[9]
                try:
                    c = float(c)
                except Exception:
                    continue
                if c != c or d is None:
                    continue
                rec.append((str(d), c))
            return pd.DataFrame(rec, columns=["date", "close"])
        except Exception:
            time.sleep(5)
    return pd.DataFrame(columns=["date", "close"])


def fetch_csi(code, deep=True):
    """中证行情：逐块落盘，断点续跑。deep=False 时只做增量（日常更新用）"""
    ck = f"csi{code}"
    old = load(ck)
    end_s = TODAY.strftime("%Y%m%d")
    if old is None or len(old) == 0:
        old = pd.DataFrame(columns=["date", "close"])
    old["date"] = old["date"].astype(str)
    have = set(old["date"])

    if deep:
        y = BACKFILL_FROM
        while y <= TODAY.year:
            a = f"{y}0101"
            b = min(f"{y + CHUNK_YEARS - 1}1231", end_s)
            # 已覆盖则跳过（区间内已有足够样本）
            span = [d for d in have if a <= d <= b]
            if len(span) >= 200 or (b < end_s and len(span) >= 60):
                y += CHUNK_YEARS
                continue
            p = csindex_api(code, a, b)
            if not p.empty:
                old = merge(old, p)
                have = set(old["date"])
                save(ck, old)
                log(f"    {code} {a}~{b}: +{len(p)} 行（累计 {len(old)}）")
            else:
                log(f"    {code} {a}~{b}: 失败，稍后重试")
            time.sleep(SLEEP)
            y += CHUNK_YEARS
    # 增量
    last = str(old["date"].max()) if len(old) else ""
    if last and last < end_s:
        p = csindex_api(code, last, end_s)
        if not p.empty:
            old = merge(old, p)
            log(f"    {code} 增量 +{len(p)} 行")
        time.sleep(SLEEP)
    if old is None or len(old) == 0:
        return None
    save(ck, old)
    return old


def csindex_indicator(code, tries=3):
    """中证官网静态估值 XLS（最近约 20 个交易日，每日更新，无速率限制）"""
    url = ("https://oss-ch.csindex.com.cn/static/html/csindex/public/uploads/file/autofile/"
           f"indicator/{code}indicator.xls")
    for i in range(tries):
        try:
            r = requests.get(url, headers={"User-Agent": UA}, timeout=30)
            if r.status_code == 200 and len(r.content) > 2000 and (
                    r.content[:2] == b"\xd0\xcf" or r.content[:4] == b"PK\x03\x04"):
                df = pd.read_excel(io.BytesIO(r.content))
                out = pd.DataFrame({
                    "date": pd.to_datetime(df.iloc[:, 0], format="%Y%m%d",
                                           errors="coerce").dt.strftime("%Y%m%d"),
                    "pe": pd.to_numeric(df.iloc[:, 6], errors="coerce"),
                    "dy": pd.to_numeric(df.iloc[:, 8], errors="coerce"),
                })
                return out.dropna(subset=["date"])
            time.sleep(4)
        except Exception:
            time.sleep(4)
    return None


# ----------------------------------------------------------- 腾讯行情
def tx_kline(code, n=2000):
    url = f"https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param={code},day,,,{n},"
    try:
        r = requests.get(url, headers={"User-Agent": UA, "Referer": "https://gu.qq.com/"}, timeout=30)
        d = (r.json() or {}).get("data") or {}
        node = d.get(code)
        rows = None
        if isinstance(node, dict):
            rows = node.get("qfqday") or node.get("day")
        elif isinstance(node, list):
            rows = node
        if not rows:
            return None
        return pd.DataFrame([(str(x[0]).replace("-", ""), float(x[2])) for x in rows],
                            columns=["date", "close"])
    except Exception:
        return None


def fetch_tx(code):
    ck = f"tx{code}"
    old = load(ck)
    p = tx_kline(code, 2000)
    if p is not None and not p.empty:
        old = merge(old, p)
    if old is None or len(old) == 0:
        return None
    save(ck, old)
    return old


def fetch_cni(code):
    """国证指数网官方行情（hq.cnindex.com.cn），单次可取 2005 年至今全历史、无限流。"""
    ck = f"cni{code}"
    old = load(ck)
    try:
        import official as OF
        p = OF.cnindex_daily(code, start="2005-01-01")
        if p is not None and not p.empty:
            old = merge(old, p)
            log(f"    国证官方 {code}: {len(p)} 行")
    except Exception as e:
        log(f"    国证官方 {code} 失败: {e}")
    if old is None or len(old) == 0:
        return None
    save(ck, old)
    return old


# ----------------------------------------------------------- 乐咕估值(月频)
def legu(kind, symbol):
    """kind: pe / pb"""
    ck = f"lg_{kind}_{symbol}"
    old = load(ck)
    try:
        import akshare as ak
        fn = ak.stock_index_pe_lg if kind == "pe" else ak.stock_index_pb_lg
        df = fn(symbol=symbol)
        col = "滚动市盈率" if kind == "pe" else "市净率"
        out = pd.DataFrame({
            "date": pd.to_datetime(df["日期"]).dt.strftime("%Y%m%d"),
            "v": pd.to_numeric(df[col], errors="coerce"),
        }).dropna()
        old = merge(old, out)
        save(ck, old)
    except Exception as e:
        log(f"    乐咕 {kind} {symbol} 失败: {e}")
    return old


def get_rates():
    """中美国债收益率。优先官方：中债登（中国）+ 美国财政部（美国），失败才降级 AkShare。"""
    ck = "rates"
    old = load(ck)
    ok = False
    try:
        import official as OF
        cn = OF.chinabond_10y(2006, None, log=log)
        us = OF.ust_10y(2006, None, log=log)
        if cn is not None and len(cn) and us is not None and len(us):
            df = pd.merge(cn, us, on="date", how="outer").sort_values("date")
            df = df.dropna(subset=["cn10y", "us10y"], how="all")
            old = merge(old, df)
            save(ck, old)
            log(f"  国债收益率[官方] 中债登 {len(cn)} 行 + 美财政部 {len(us)} 行")
            ok = True
        else:
            log("  官方债券源不完整，降级 AkShare")
    except Exception as e:
        log(f"  官方债券源失败: {e}")
    if not ok:
        try:
            import akshare as ak
            df = ak.bond_zh_us_rate()[["日期", "中国国债收益率10年", "美国国债收益率10年"]].copy()
            df.columns = ["date", "cn10y", "us10y"]
            df["date"] = pd.to_datetime(df["date"]).dt.strftime("%Y%m%d")
            old = merge(old, df.dropna())
            save(ck, old)
            log("  国债收益率[AkShare 兜底]")
        except Exception as e:
            log(f"  国债收益率失败: {e}")
    return old


# ----------------------------------------------------------- 抓全部行情
def fetch_all(deep=True):
    data, used = {}, {}
    log("=== 抓取行情 ===")
    for key, (name, cands) in SERIES.items():
        got, src = None, None
        for st, code, same in cands:
            if st == "csi":
                df = fetch_csi(code, deep=deep)
            elif st == "cni":
                df = fetch_cni(code)
            else:
                df = fetch_tx(code)
            if df is None or len(df) < 250:
                continue
            if got is None:
                got, src = df.copy(), (st, code)
            elif same:
                # 同一指数的不同数据源：合并以拉长历史
                m = merge(got, df)
                if m is not None and len(m) >= len(got):
                    got = m
                    src = (src[0] + "+", f"{src[1]}/{code}")
        if got is None:
            log(f"  !! {name} 所有源均失败")
            continue
        got = got.drop_duplicates("date", keep="last").sort_values("date").reset_index(drop=True)
        data[key] = got
        used[key] = src
        log(f"  {name}({key}) <- {src[0]}:{src[1]}  {len(got)} 行  "
            f"{got['date'].iloc[0]}~{got['date'].iloc[-1]}")
    return data, used


# ----------------------------------------------------------- 指标工具
def ser(df):
    return pd.Series(df["close"].astype(float).values,
                     index=pd.to_datetime(df["date"], format="%Y%m%d", errors="coerce"))


def has_upto(S, key, min_date="2020-01-01", min_rows=250):
    """该序列是否已回补到 min_date 之后。

    关键：新增的官方全收益指数若被 WAF 截断（例如只到 2016 年），
    直接拿来算 M10（2016 年起的回归）会导致样本不足甚至模块失效，
    因此必须先做充分性校验，不够就退回价格指数。
    """
    s = S.get(key)
    if s is None or len(s) < min_rows:
        return False
    try:
        return s.index[-1] >= pd.Timestamp(min_date)
    except Exception:
        return False


def roll_annual(s, days=1095, year=365.0):
    idx, v = list(s.index), s.values.astype(float)
    out = np.full(len(v), np.nan)
    for i in range(len(v)):
        j = bisect.bisect_left(idx, idx[i] - pd.Timedelta(days=days))
        if j <= 0 or j > i:
            continue
        if v[j] > 0 and v[i] > 0:
            out[i] = (v[i] / v[j]) ** (year / days) - 1
    return pd.Series(out, index=s.index)


def diff_n(a, b, n=40):
    return ((a / a.shift(n) - 1) - (b / b.shift(n) - 1)) * 100.0


def boll(s, n=252, k=2.0):
    m, sd = s.rolling(n).mean(), s.rolling(n).std(ddof=0)
    return m, m + k * sd, m - k * sd


def pack(idx, *ss, nd=4):
    cols = []
    for s in ss:
        if s is None:
            cols.append([None] * len(idx))
            continue
        v = np.asarray(s.reindex(idx).values, dtype=float)
        cols.append([None if x != x else round(float(x), nd) for x in v])
    return {"dates": [d.strftime("%Y-%m-%d") for d in idx], "cols": cols}


def trim(o, start):
    k = bisect.bisect_left(o["dates"], start)
    return o if k <= 0 else {"dates": o["dates"][k:],
                             "cols": [c[k:] for c in o["cols"]]}


def thin(obj, keep=420, step=3):
    """最近 keep 个交易日保留全量，更早的每 step 天取一个，压缩体积"""
    ds = obj.get("dates") or []
    n = len(ds)
    if n <= keep + 400:
        return obj
    idx = sorted(set(list(range(0, n - keep, step)) + list(range(n - keep, n))))
    return {"dates": [ds[i] for i in idx],
            "cols": [[c[i] for i in idx] for c in obj["cols"]]}


def lv(*ss):
    out = []
    for s in ss:
        if s is None:
            out.append(None)
            continue
        t = s.dropna()
        out.append(None if t.empty else round(float(t.iloc[-1]), 4))
    return out


# ----------------------------------------------------------- 真实偏债混合基准（基金累计复权净值）
def fetch_fund_nav(code):
    """真实偏债混合基准：基金「累计净值」(含分红再投资≈总回报)。

    万得偏债混合型基金指数(885003.WI)为万得专有数据，无免费官方源；
    885003 本身是全体偏债混合型基金的等权平均，故用一只长期、经典的
    偏债混合型基金——南方宝元债券A(202101, 2002 年成立)——的累计复权
    净值作为真实基准替代，远优于此前「债基指数+沪深300」的错误合成。
    数据经 AkShare(东财基金) 获取，缓存断点续传。
    """
    ck = f"fund{code}"
    old = load(ck)
    try:
        import akshare as ak
        df = ak.fund_open_fund_info_em(symbol=code, indicator="累计净值走势")
        if df is not None and len(df):
            df = df[["净值日期", "累计净值"]].copy()
            df["date"] = pd.to_datetime(df["净值日期"]).dt.strftime("%Y%m%d")
            df["nav"] = pd.to_numeric(df["累计净值"], errors="coerce")
            df = df[["date", "nav"]].dropna()
            new = merge(old, df) if old is not None else df
            save(ck, new)
            old = new
            log(f"  基金{code}净值 {len(old)} 行 {old['date'].iloc[0]}~{old['date'].iloc[-1]}")
    except Exception as e:
        log(f"  基金{code}净值失败: {e}")
    return old


def fetch_fund_fq(code, name=""):
    """抓取场外指数基金的「复权（分红再投资）」总回报序列。

    用天天基金(东财)日增长率(equityReturn)复利——该字段本身已按分红/份额折算
    调整，复利即投资者真实总回报（含分红再投），缓存到 cache/fund{code}_fq.csv。
    返回 (date, close) 的 DataFrame，close 为 1000 起算的复权值。
    """
    ck = f"fund{code}_fq"
    old = load(ck)
    try:
        import requests as _rq
        import re as _re
        url = f"http://fund.eastmoney.com/pingzhongdata/{code}.js"
        h = {"Referer": f"http://fund.eastmoney.com/{code}.html",
             "User-Agent": UA}
        t = _rq.get(url, timeout=20, headers=h).text
        m = _re.search(r"Data_netWorthTrend\s*=\s*(\[.*?\]);", t, _re.S)
        if m:
            import json as _json
            nw = _json.loads(m.group(1))
            rows = []
            for d in nw:
                dt = pd.to_datetime(d["x"], unit="ms").normalize().strftime("%Y%m%d")
                er = d.get("equityReturn")
                if er in ("", None):
                    continue
                rows.append((dt, float(er)))
            if len(rows) > 120:
                df = pd.DataFrame(rows, columns=["date", "er"]).drop_duplicates("date")
                df = df.sort_values("date").reset_index(drop=True)
                df["close"] = 1000.0 * (1 + df["er"] / 100.0).fillna(1.0).cumprod()
                new = df[["date", "close"]]
                old = merge(old, new) if old is not None else new
                save(ck, old)
                log(f"  基金{code}复权 {len(old)} 行 {old['date'].iloc[0]}~{old['date'].iloc[-1]}")
    except Exception as e:
        log(f"  基金{code}复权失败: {e}")
    return old


def fetch_sw_fund_index(code="807330"):
    """申万宏源基金指数（官方公开发布）日收盘，缓存 cache/sw{code}.csv（date=YYYYMMDD, nav）

    这是本项目找到的、最接近万得偏债混合型基金指数(885003.WI)的**公开真实数据源**：
    807330 = 申万宏源混合偏债基金指数，即「偏债混合型基金」这一分类的等权/加权平均，
    与 Wind 885003.WI 的编制对象一致（Wind 侧为专有数据、无免费接口）。
    数据起点 2009-12-31；2004-12-31 ~ 2009-12-30 段由本模块用 25/75 合成形态回补。
    """
    ck = f"sw{code}"
    old = load(ck)
    try:
        import requests as _rq
        import urllib3 as _u3
        _u3.disable_warnings()
        url = "https://www.swsresearch.com/insWechatSw/fundIndex/getFundKChartData"
        hd = {"User-Agent": "Mozilla/5.0", "Content-Type": "application/json"}
        # 申万站点证书链在本机会校验失败（CERTIFICATE_VERIFY_FAILED），故关闭校验
        r = _rq.post(url, json={"swIndexCode": str(code), "type": "DAY"},
                     headers=hd, timeout=40, verify=False)
        rows = (r.json() or {}).get("data") or []
        if rows:
            df = pd.DataFrame(rows)
            df = df[["bargaindate", "closeindex"]].copy()
            df["date"] = pd.to_datetime(df["bargaindate"]).dt.strftime("%Y%m%d")
            df["nav"] = pd.to_numeric(df["closeindex"], errors="coerce")
            df = df[["date", "nav"]].dropna()
            new = merge(old, df) if old is not None else df
            save(ck, new)
            old = new
            log(f"  申万基金指数{code} {len(old)} 行 {old['date'].iloc[0]}~{old['date'].iloc[-1]}")
    except Exception as e:
        log(f"  申万基金指数{code}失败: {e}")
    return old


def fetch_zzqz985():
    """中证全指(000985)日收盘，走中证官网分块回填(2005 起)，缓存 cache/zzqz985.csv（date=YYYYMMDD, close）"""
    ck = "zzqz985"
    old = load(ck)
    if old is None or len(old) == 0:
        old = pd.DataFrame(columns=["date", "close"])
    old["date"] = old["date"].astype(str)
    have = set(old["date"])
    end_s = TODAY.strftime("%Y%m%d")
    y = BACKFILL_FROM
    while y <= TODAY.year:
        a = f"{y}0101"
        b = min(f"{y + CHUNK_YEARS - 1}1231", end_s)
        # 按年粒度检查：块级计数会掩盖年度空洞。
        # 实例：2011 年整年缺失，却被所属 2011-2016 块的 1112 行判定为"数据已足够"而跳过，
        # 该段随后被 ffill 填成常量直线，图上表现为"不涨不跌"的假横盘。
        years = list(range(y, min(y + CHUNK_YEARS, TODAY.year + 1)))
        thin = []
        for yy in years:
            ya, yb = f"{yy}0101", min(f"{yy}1231", end_s)
            n = len([d for d in have if ya <= d <= yb])
            thr = 200 if yy != TODAY.year else max(20, int(200 * max(TODAY.month - 1, 1) / 12))
            if n < thr:
                thin.append((yy, ya, yb, n, thr))
        if not thin:
            y += CHUNK_YEARS
            continue
        # 按年单独补抓：中证 API 单次返回存在行数上限，按 6 年整块抓会被截断、
        # 只落库靠后的几年 —— 这正是 2011 年整年丢失的根因。
        for yy, ya, yb, n, thr in thin:
            log(f"  中证全指000985 {yy} 年仅 {n} 行(<{thr})，定向补抓")
            p = csindex_api("000985", ya, yb)
            if p is not None and not p.empty:
                old = merge(old, p)
                have = set(old["date"])
                save(ck, old)
                log(f"  中证全指000985 {ya}~{yb}: +{len(p)} 行（累计 {len(old)}）")
            else:
                log(f"  中证全指000985 {ya}~{yb}: 失败，稍后重试")
            time.sleep(SLEEP)
        y += CHUNK_YEARS
    # 增量
    last = str(old["date"].max()) if len(old) else ""
    if last and last < end_s:
        p = csindex_api("000985", last, end_s)
        if p is not None and not p.empty:
            old = merge(old, p)
            log(f"  中证全指000985 增量 +{len(p)} 行")
        time.sleep(SLEEP)
    if old is None or len(old) == 0:
        return None
    save(ck, old)
    s = pd.Series(old["close"].astype(float).values, index=pd.to_datetime(old["date"]))
    return s[~s.index.duplicated(keep="last")].sort_index()


# ----------------------------------------------------------- 主流程
# ----------------------------------------------------------- 成交集中度辅助
def _tencent_amounts(codes):
    """腾讯实时快照(qt.gtimg.cn)拿全部A股当日真实成交额。返回 {code: 成交额(元)}。

    两个易错点（曾导致 M13 每日增量静默失效）：
      1) qt.gtimg.cn 的 f[37] 单位是【万元】，而历史回补 tc_raw/tc_recent 用【元】
         -> 必须 *1e4 统一到元，否则写入后单位混用会污染周/月窗口聚合。
      2) f[1] 是股票【名称】，不是代码 -> 键必须取响应变量名 v_sh600000 -> sh600000，
         否则 tc_raw 会写出「浦发银行.csv」这类错名文件。
    """
    out = {}
    if not codes:
        return out
    B = 600
    for i in range(0, len(codes), B):
        chunk = codes[i:i + B]
        try:
            r = requests.get("https://qt.gtimg.cn/q=" + ",".join(chunk),
                             headers={"User-Agent": UA}, timeout=25)
            for line in r.text.split(";"):
                line = line.strip()
                if not line or "=" not in line:
                    continue
                _var = line.split("=")[0].strip()
                code = _var[2:] if _var.startswith("v_") else _var
                val = line.split("=", 1)[1].strip().strip('"')
                f = val.split("~")
                if len(f) <= 37:
                    continue
                try:
                    amt = float(f[37]) * 10000.0   # 万元 -> 元
                except Exception:
                    continue
                if amt > 0 and len(code) > 2:
                    out[code] = amt
        except Exception:
            time.sleep(1)
    return out


def _tencent_snap(codes):
    """腾讯实时快照的 (交易日, 快照时间HHMMSS)。休市期间返回最近交易日。"""
    try:
        r = requests.get("https://qt.gtimg.cn/q=" + ",".join(codes[:80]),
                         headers={"User-Agent": UA}, timeout=15)
        for line in r.text.split(";"):
            if "=" not in line:
                continue
            val = line.split("=", 1)[1].strip().strip('"')
            f = val.split("~")
            if len(f) > 31:
                d, t = "", ""
                s = (f[30] or "").strip()
                # 布局A（实测现行）：f[30]='YYYYMMDDHHMMSS' 14位，日期时间合一
                if len(s) == 14 and s.isdigit():
                    d, t = s[:8], s[8:14]
                # 布局B（旧）：f[30]='YYYYMMDD' 8位 + f[31] 日期时间
                elif len(s) == 8 and s.isdigit():
                    d = s
                    s2 = (f[31] or "").strip()
                    if len(s2) == 14 and s2.isdigit():
                        t = s2[8:14]
                    elif len(s2) == 6 and s2.isdigit():
                        t = s2
                    elif len(s2) >= 14 and s2[:8].isdigit() and s2[8:14].isdigit():
                        t = s2[8:14]
                if len(d) == 8:
                    return f"{d[:4]}-{d[4:6]}-{d[6:8]}", t
    except Exception:
        pass
    return None, ""


def _wkmk(date_str):
    iso = dt.date.fromisoformat(date_str).isocalendar()
    return iso[0] * 100 + iso[1]


def _top5_ratio_of(vals, total):
    """vals 为窗口内各股票累计额(>0)，返回前5%占比%。"""
    if total <= 0 or len(vals) == 0:
        return None
    k = max(1, int(round(len(vals) * 0.05)))
    s = np.sort(vals)
    return round(float(s[-k:].sum()) / total * 100.0, 3)


def _freq_advance(fname, snap, buf, kind):
    """周/月窗口推进：snap 所在窗口(周/月)的最新累计值，同窗口更新末点、跨窗口追加新点。"""
    p = os.path.join(CACHE, fname)
    if not os.path.exists(p):
        return
    def kof(d):
        return _wkmk(d) if kind == "w" else d[:7]
    key = kof(snap)
    try:
        df = pd.read_csv(p, dtype={"date": str})
        if len(df) == 0:
            return
        last = str(df["date"].iloc[-1])
        lk = kof(last)
        if key < lk:
            return
        sub = buf[buf["date"].map(kof) == key]
        if len(sub) == 0:
            return
        g = sub.groupby("code")["amount"].sum()
        g = g[g > 0]
        if len(g) == 0:
            return
        v = np.sort(g.to_numpy(dtype=float))
        rr = _top5_ratio_of(v, float(v.sum()))
        if rr is None:
            return
        row_date = str(sub["date"].max())
        if key == lk:
            df.loc[df.index[-1], "date"] = row_date
            df.loc[df.index[-1], "ratio"] = rr
        else:
            df.loc[len(df)] = [row_date, rr]
        df.to_csv(p, index=False)
    except Exception:
        pass


# 单位约定：tc_backfill.py 用「新浪 volume(股) × 100 × close」，其量值是真实成交额的
# 100 倍（tc_backfill 注释误以为 volume 是「手」）。历史 tc_raw/tc_recent 全部是这套
# 100× 口径；日度集中度是比值（规模无关）所以不受影响，但【周/月窗口要跨日求和】，
# 一旦混入真实「元」就会把当日权重压到 1%，窗口值失真。
# 故腾讯实时真实成交额写入缓冲区前统一 ×100 对齐历史口径。
# 若日后重跑 tc_backfill 全史回补，务必同时把此处 _TC_UNIT 改回 1.0。
_TC_UNIT = 100.0


def _tc_roll_maintain(snap, amt, codes):
    """完整交易日 snap 到达后的配套维护：tc_recent 滚动缓冲、周/月窗口推进、tc_raw 明细追加。
    全部静默容错——失败不影响主流程（下次 run 补）。"""
    # 1) tc_recent（最近45交易日全市场缓冲，供周/月聚合与未来重算）
    try:
        rp = os.path.join(CACHE, "tc_recent.csv")
        recent = pd.read_csv(rp, dtype={"date": str, "code": str}) if os.path.exists(rp) else None
        if recent is not None and len(recent):
            recent = recent[recent["date"] != snap]
        new = pd.DataFrame({"date": [snap] * len(amt),
                            "code": list(amt.keys()),
                            "amount": [float(x) * _TC_UNIT for x in amt.values()]})
        buf = pd.concat([recent, new], ignore_index=True) if (recent is not None and len(recent)) else new
        ds = sorted(buf["date"].unique())
        keep = set(ds[-45:])
        buf = buf[buf["date"].isin(keep)]
        buf.to_csv(rp, index=False)
        _freq_advance("tc_concentration_weekly.csv", snap, buf, kind="w")
        _freq_advance("tc_concentration_monthly.csv", snap, buf, kind="m")
    except Exception:
        pass
    # 2) tc_raw 明细 append 当日（与历史回补文件同构，保持全史至最新）
    #    幂等与提速（2026-09-17 同盘基准：单文件 append 仅 0.5ms，全市场约 3s）：
    #      ① 标记文件记录「已写入的交易日」——同日重跑直接整体跳过，5552 个文件零 I/O（省 ~12s 扫描）；
    #      ② 逐文件再查末 256 字节兜底（防标记丢失或上一轮半途失败）。
    #    注：不要改成线程池——实测 24 线程反而 0.77x（小文件 I/O 已是瓶颈外的开销）。
    mark = os.path.join(CACHE, "tc_raw_last.txt")
    try:
        if os.path.exists(mark) and (open(mark, encoding="utf-8").read() or "").strip() == snap:
            return
    except Exception:
        pass
    todo = 0
    ok = 0
    for code, a in amt.items():
        try:
            p = os.path.join(RAW, code + ".csv")
            with open(p, "rb") as fh:
                fh.seek(0, os.SEEK_END)
                sz = fh.tell()
                fh.seek(max(0, sz - 256))
                tail = fh.read().decode("utf-8", "ignore")
            if f"{snap}," in tail:
                continue
            todo += 1
            with open(p, "a", encoding="utf-8") as fh:
                fh.write(f"{snap},{a * _TC_UNIT:.6g}\n")
            ok += 1
        except Exception:
            continue
    # 失败率 >5% 时不落标记，留给下一轮重试
    if ok >= todo * 0.95:
        try:
            with open(mark, "w", encoding="utf-8") as fh:
                fh.write(snap)
        except Exception:
            pass


def _csi_000985_ohlc():
    """中证全指(000985) 官方日线OHLC：csindex perf API → 本地缓存 tc_000985_ohlc.csv。

    覆盖 2005-01-04 至今（5265+ 交易日，基日 2004-12-31=1000，官方权威源）。
    缓存末日早于今天才增量刷新（拉「末日+1→今天」小段），缓存缺失才全量拉；
    网络失败一律静默退回旧缓存 —— 保证每日渲染不依赖任何实时第三方。
    返回 [(date, open, close, low, high), ...] 升序。
    """
    cache = os.path.join(CACHE, "tc_000985_ohlc.csv")
    rows = []
    if os.path.exists(cache):
        try:
            _df = pd.read_csv(cache, dtype={"date": str})
            rows = [(str(r["date"]), float(r["open"]), float(r["close"]),
                     float(r["low"]), float(r["high"])) for _, r in _df.iterrows()]
        except Exception:
            rows = []
    _need = True
    if rows:
        _need = rows[-1][0] < dt.date.today().isoformat()
    if _need:
        try:
            if rows:
                # 起点回退 5 天（带重叠）：csindex 的 startDate 落在非交易日（如周末）时
                # 会整段返回空 data，用「末日+1」会静默漏掉最新交易日。重叠几天无害。
                _d0 = dt.date.fromisoformat(rows[-1][0]) - dt.timedelta(days=5)
                _start = _d0.strftime("%Y%m%d")
            else:
                _start = "20040101"
            _end = dt.date.today().strftime("%Y%m%d")
            r = requests.get("https://www.csindex.com.cn/csindex-home/perf/index-perf",
                             params={"indexCode": "000985", "startDate": _start,
                                     "endDate": _end, "pageNum": 1, "pageSize": 20000},
                             headers={"User-Agent": UA}, timeout=90)
            if r.status_code == 200:
                _all = {d: (o, c, l, h) for d, o, c, l, h in rows}
                for x in (r.json() or {}).get("data") or []:
                    _dd = x.get("tradeDate", "")
                    if len(_dd) != 8:
                        continue
                    _o, _h, _l, _c = x.get("open"), x.get("high"), x.get("low"), x.get("close")
                    if None in (_o, _h, _l, _c):
                        continue
                    _day = f"{_dd[:4]}-{_dd[4:6]}-{_dd[6:8]}"
                    _all[_day] = (float(_o), float(_c), float(_l), float(_h))
                rows = sorted((d,) + v for d, v in _all.items())
                with open(cache, "w", encoding="utf-8") as f:
                    f.write("date,open,close,low,high\n")
                    for d, o, c, l, h in rows:
                        f.write(f"{d},{o},{c},{l},{h}\n")
        except Exception:
            pass  # 网络/WAF失败 → 沿用旧缓存
    # 兜底：中证官网被 WAF 限流时，用腾讯日线补齐最新几个交易日（sh000985 自 2012-06 起有效）
    try:
        if rows and rows[-1][0] < dt.date.today().isoformat():
            r = requests.get("https://web.ifzq.gtimg.cn/appstock/app/fqkline/get",
                             params={"param": "sh000985,day,,,10,"},
                             headers={"User-Agent": UA, "Referer": "https://gu.qq.com/"},
                             timeout=30)
            node = ((r.json() or {}).get("data") or {}).get("sh000985")
            kk = (node.get("qfqday") or node.get("day")) if isinstance(node, dict) else None
            _all = {d: (o, c, l, h) for d, o, c, l, h in rows}
            for x in kk or []:
                if len(x) < 5:
                    continue
                _all[str(x[0])] = (float(x[1]), float(x[2]), float(x[4]), float(x[3]))
            _new = sorted((d,) + v for d, v in _all.items())
            if _new != rows:
                rows = _new
                with open(cache, "w", encoding="utf-8") as f:
                    f.write("date,open,close,low,high\n")
                    for d, o, c, l, h in rows:
                        f.write(f"{d},{o},{c},{l},{h}\n")
                log(f"    000985 腾讯兜底补齐至 {rows[-1][0]}")
    except Exception:
        pass
    return rows


def main(deep=True):
    data, used = fetch_all(deep=deep)
    S = {k: ser(v) for k, v in data.items() if len(v)}

    log("=== 抓取估值与利率 ===")
    rates = get_rates()
    swnav = fetch_sw_fund_index("807330")     # 申万宏源混合偏债基金指数：885003.WI 的公开等价物
    eynav = fetch_fund_nav("110017")          # 易方达增强回报债券A：@qzy69 原始一级债基口径
    if eynav is not None and len(eynav):
        yf = pd.Series(eynav["nav"].values,
                       index=pd.to_datetime(eynav["date"], format="%Y%m%d")).sort_index()
    else:
        yf = None
    pe300, pb300 = legu("pe", "沪深300"), legu("pb", "沪深300")
    pe015 = legu("pe", "上证红利")
    ind300 = csindex_indicator("000300")     # 中证官网 日频 PE/股息率
    ind922 = csindex_indicator("000922")
    ind015 = csindex_indicator("000015")
    for nm, x in [("ind300", ind300), ("ind922", ind922), ("ind015", ind015)]:
        log(f"  {nm}: {0 if x is None else len(x)} 行")

    out = {"updated": dt.datetime.now().strftime("%Y-%m-%d %H:%M"), "modules": {}, "fellback": []}
    M = out["modules"]
    SRC = {}   # 记录各模块实际采用的口径，供「口径说明」动态生成

    log("=== 计算指标 ===")

    # ---------- M1 五年之锚 ----------
    # 标的：原文标题即「Wind全A五年之锚」→ 正宗口径应为<b>万得全A(881001.WI)</b>，
    # 经 Wind 官方 API 抓取全历史(1999-12-30 起，见 _wind881001.py)。
    # 相比此前替代用的中证A股(930903, 2005 起)，可把五年均线提前到 2004-12、分位曲线提前到 2006-01。
    _s881 = None
    _p881 = os.path.join(CACHE, "wind881001.csv")
    if os.path.exists(_p881):
        try:
            _d881 = pd.read_csv(_p881, dtype={"date": str})
            _s881 = pd.Series(_d881["close"].astype(float).values,
                              index=pd.to_datetime(_d881["date"], format="%Y-%m-%d")).sort_index()
            _s881 = _s881[~_s881.index.duplicated()]
        except Exception as e:
            log(f"  万得全A缓存读取失败: {e}")
            _s881 = None
    if _s881 is not None and len(_s881) > 1500:
        s = _s881
        SRC["m1"] = "万得全A 881001.WI（Wind 官方，1999-12-30 起）"
    else:
        s = S.get("930903")
        SRC["m1"] = "中证A股 930903（万得全A缓存缺失时的替代）"
    if s is not None and len(s):
        ma = s.rolling(1250).mean()
        dev = (s / ma - 1) * 100
        # 原文口径：五年之锚 = 五年均线下浮 20%（MA×0.8，抄底线）。
        # 低位抄底看「曲线是否触及/跌破 MA-20」；顶部风险看「向上偏离 MA」的幅度分档。
        ma20 = ma * 0.8
        dev_ma20 = (s / ma20 - 1) * 100 if (len(s) >= 1250) else np.full(len(s), np.nan)
        # 10 年滚动偏离度百分位（辅助风险维度，保留）
        W10 = 2500
        MINP = 250
        _v = dev.values.astype(float)
        _n = len(_v)
        _p = np.full(_n, np.nan)
        for _i in range(_n):
            _cur = _v[_i]
            if _cur != _cur:          # 当前值本身是 NaN（五年均线尚未热身）→ 跳过
                continue
            _lo = _i - W10 + 1
            if _lo < 0:
                _lo = 0
            _w = _v[_lo:_i + 1]
            _w = _w[~np.isnan(_w)]    # 只统计窗口内真实有效的历史样本
            if len(_w) < MINP:        # 有效样本不足 1 年 → 不出值
                continue
            _p[_i] = float((_w[:-1] <= _cur).sum()) / (len(_w) - 1) * 100.0
        pct10 = pd.Series(_p, index=dev.index)
        # 曲线首个有值日期（供前端标注口径）
        _pfirst = pct10.first_valid_index()
        M_PCT_START = _pfirst.strftime("%Y-%m") if _pfirst is not None else ""
        M["m1"] = {"data": pack(s.index, s, ma, pct10),
                   "cur": {"close": lv(s)[0], "ma1250": lv(ma)[0], "dev": lv(dev)[0],
                           "pct10": lv(pct10)[0],
                           "pctStart": M_PCT_START,
                           "date": s.index[-1].strftime("%Y-%m-%d")}}
        log(f"  M1 五年之锚 OK（{SRC['m1']}，分位起自 {M_PCT_START}）")

    # ---------- M1b 五年之锚（原文口径：五年均线下浮20%抄底线） ----------
    # 对比用版本，严格贴合张翼轸原文：
    #   抄底判定 = 万得全A 指数是否「触及/跌破 五年均线 × 0.8」（MA-20），
    #   顶部风险 = 「向上偏离五年均线」的幅度分档（文字口径：100%+ / ~68% / ~35% / 当前17%中性）。
    # 图表三线：万得全A、五年均线(MA)、五年均线下浮20%(MA×0.8)。
    if 'm1' in M and s is not None and len(s) >= 1250:
        _ma2 = M['m1']['data']
        # 图中三条参考带：五年均线(MA)、五年均线下浮20%(MA×0.8，抄底线)、五年均线上浮30%(MA×1.3，顶部带)。
        _ma20_ = ma * 0.8
        _upb = ma * 1.3
        # 图上穿越箭头：当日相对均线偏离 (收盘-均线)/均线 的区间外标记。
        # 偏离 ≤ -15% → 红色上箭头⬆(抄底/买入区)；偏离 ≥ +30% → 绿色下箭头⬇(顶部/减仓区)。
        # 同方向 60 个交易日(约一季度)只标一次，避免过于密集。
        _THR_BUY, _THR_SELL = -15.0, 30.0   # 买入/卖出阈值（偏离%）
        _CD = 60                      # 同方向 60 个交易日内只标一次
        _buys, _sells = [], []
        _cdb = _cds = 0                # 买/卖的剩余冷却交易日
        _dv = dev.values
        for _i in range(len(_dv)):
            _v = _dv[_i]
            if _v != _v:               # 均线未热身段：无信号，但冷却计数照常递减
                _cdb = 0 if _cdb <= 0 else _cdb - 1
                _cds = 0 if _cds <= 0 else _cds - 1
                continue
            # 买入：偏离 ≤ -15% 标红色上箭头，之后 20 个交易日不再重复
            if _v <= _THR_BUY and _cdb <= 0:
                _buys.append([s.index[_i].strftime("%Y-%m-%d"), round(float(s.iloc[_i]), 4)])
                _cdb = _CD
            else:
                _cdb = 0 if _cdb <= 0 else _cdb - 1
            # 卖出：偏离 ≥ +30% 标绿色下箭头，同样 20 个交易日冷却
            if _v >= _THR_SELL and _cds <= 0:
                _sells.append([s.index[_i].strftime("%Y-%m-%d"), round(float(s.iloc[_i]), 4)])
                _cds = _CD
            else:
                _cds = 0 if _cds <= 0 else _cds - 1
        M["m1b"] = {"data": pack(s.index, s, ma, _ma20_, _upb),
                    "marks": {"buys": _buys, "sells": _sells,
                              "buy_thr": _THR_BUY, "sell_thr": _THR_SELL},
                    "cur": {"close": lv(s)[0], "ma1250": lv(ma)[0], "ma20": lv(_ma20_)[0],
                            "dev": lv(dev)[0], "dev_ma20": lv(dev_ma20)[0],
                            "date": s.index[-1].strftime("%Y-%m-%d")}}
        log(f"  M1b 五年之锚（原文口径）OK（⬆{len(_buys)}次 / ⬇{len(_sells)}次）")

    # ---------- M2 中美股债性价比 + 市赚率 ----------
    if pe300 is not None and len(pe300) and rates is not None and len(rates):
        pe = pd.Series(pe300["v"].values,
                       index=pd.to_datetime(pe300["date"], format="%Y%m%d")).sort_index()
        rt = rates.copy()
        rt.index = pd.to_datetime(rt["date"], format="%Y%m%d")
        cn = rt["cn10y"].astype(float)
        us = rt["us10y"].astype(float)
        idx = pe.index
        cn, us = cn.reindex(idx).ffill(), us.reindex(idx).ffill()
        ey = 100.0 / pe
        ey_cn, ey_us = ey - cn, ey - us
        pb = None
        if pb300 is not None and len(pb300):
            pb = pd.Series(pb300["v"].values,
                           index=pd.to_datetime(pb300["date"], format="%Y%m%d")).sort_index()
        if pb is not None:
            roe = (pb.reindex(idx).ffill() / pe).dropna()
            pr = (pe / (roe * 100)).dropna()
        else:
            roe, pr = None, None
        # 用中证官网日频 PE 校准最新值
        pe_now = None
        if ind300 is not None and len(ind300):
            pe_now = float(ind300["pe"].iloc[0])
        M["m2"] = {"data": pack(idx, ey_cn, ey_us, pe, pr if pr is not None else None),
                   "cur": {"pe": lv(pe)[0], "pe_daily": pe_now,
                           "cn10y": lv(cn)[0], "us10y": lv(us)[0],
                           "ey_cn": lv(ey_cn)[0], "ey_us": lv(ey_us)[0],
                           "pr": lv(pr)[0] if pr is not None else None,
                           "roe": lv(roe)[0] if roe is not None else None,
                           "date": idx[-1].strftime("%Y-%m-%d")}}
        log("  M2 股债性价比 OK")

    # ---------- M12 沪深300 估值一图看全（走势 + PE TTM + 市赚率）----------
    # 来源：EarlETF《沪深300 估值一图看全，及一个坏消息》(2024-10-28)
    # ① 沪深300全收益(H00300)走势 —— 与下方估值指标对照，研判「猜顶」与「估底」
    # ② 市盈率 TTM —— 所有估值体系的起点
    # ③ 市赚率 PR = PE / ROE —— 雪球 @ericwarn丁宁 提出，1PR=合理，>1PR 高估，<1PR 低估
    if pe300 is not None and len(pe300) and pb300 is not None and len(pb300):
        pe12 = pd.Series(pe300["v"].values,
                         index=pd.to_datetime(pe300["date"], format="%Y%m%d")).sort_index()
        pb12 = pd.Series(pb300["v"].values,
                         index=pd.to_datetime(pb300["date"], format="%Y%m%d")).sort_index()
        idx12 = pe12.index
        roe12 = (pb12.reindex(idx12).ffill() / pe12).dropna()
        pr12 = (pe12 / (roe12 * 100)).dropna()
        lvl12 = None
        if "H00300" in S:
            lvl12 = S["H00300"].reindex(idx12).ffill()
        if lvl12 is not None and lvl12.notna().sum() > 100:
            def _pct(series, val):
                s = pd.Series(series).dropna()
                return float((s <= val).sum()) / len(s) * 100.0 if len(s) else None
            cur_pe = lv(pe12)[0]
            cur_pr = lv(pr12)[0]
            M["m12"] = {"data": pack(idx12, lvl12, pe12, pr12),
                        "cur": {"hs300": lv(lvl12)[0], "pe": cur_pe,
                                "pe_pct": _pct(pe12, cur_pe),
                                "pr": cur_pr, "pr_pct": _pct(pr12, cur_pr),
                                "roe": lv(roe12)[0],
                                "date": idx12[-1].strftime("%Y-%m-%d")}}
            log("  M12 沪深300估值一图看全 OK")

    # ---------- M3 韭圈儿神奇指标 ----------
    # 原文：沪深300全收益(H00300) ÷ 万得偏债混合型基金指数(885003.WI)，
    # 两线同起点 rebased；偏离度 = 比值 − 1，0 轴=底部、+60%=顶部。
    #
    # 【沪深300 线】按原文口径用**指数基金**（非全收益指数）——取成立最早的
    # 沪深300场外指数基金：嘉实沪深300ETF联接(160706,2005-08-29成立)。
    # 用其「复权」总回报（分红再投资）序列，因含费率、贴近真实投资人回报，
    # 牛市略高于偏债混合、熊市低点自然跌破偏债线（2024-09 低点即破 0）。
    #
    # 【偏债 885003】为万得专有、无免费源；优先 Wind 真实缓存，否则回退
    # 申万宏源混合偏债基金指数(807330,2009起真实)+25/75 合成回补(2005~2009)。
    ek = "H00300" if "H00300" in S else None
    if ek and "H11023" in S:
        ix0 = S[ek].index.intersection(S["H11023"].index)   # 合成回补所需的基金交易日轴
        eq = S[ek]                                          # 沪深300 线：官方全收益指数 H00300
        # 偏债基线：优先真实 Wind 885003
        syn_lvl, bench_src = None, None
        wind885_p = os.path.join(CACHE, "wind885003.csv")
        if os.path.exists(wind885_p):
            try:
                wd = pd.read_csv(wind885_p, parse_dates=["date"])
                ws = pd.Series(pd.to_numeric(wd["close"], errors="coerce").values,
                               index=pd.to_datetime(wd["date"])).dropna().sort_index()
                if len(ws) > 250:
                    syn_lvl = ws
                    bench_src = "万得偏债混合型基金指数(885003.WI, Wind真实)"
            except Exception as e:
                log(f"  M3 读取 Wind 885003 失败: {e}")
        # 回退：申万 807330（真实,2009起）+ 25/75 合成回补（2005~2009）
        if syn_lvl is None:
            a = eq.reindex(ix0).ffill(); b = S["H11023"].reindex(ix0).ffill()
            baseX = pd.Timestamp("2004-12-31")
            ha = a.asof(baseX) if pd.notna(a.asof(baseX)) else a.iloc[0]
            hb = b.asof(baseX) if pd.notna(b.asof(baseX)) else b.iloc[0]
            syn_lvl = (0.25 * (a / ha) + 0.75 * (b / hb)) * 1000.0
            if swnav is not None and len(swnav) > 250:
                sw = pd.Series(pd.to_numeric(swnav["nav"], errors="coerce").values,
                               index=pd.to_datetime(swnav["date"], format="%Y%m%d")).dropna().sort_index()
                join = pd.Timestamp("2009-12-31")
                swj = sw.asof(join); synj = syn_lvl.asof(join)
                if pd.notna(swj) and pd.notna(synj) and synj > 0:
                    back = syn_lvl[syn_lvl.index < join] / synj * swj
                    syn_lvl = pd.concat([back, sw[sw.index >= join]]).sort_index()
                    syn_lvl = syn_lvl[~syn_lvl.index.duplicated()]
                    bench_src = "申万混合偏债指数(807330,2009起真实)+25/75合成回补"
                else:
                    bench_src = "25%沪深300全收益+75%H11023(买入持有)合成"
            else:
                bench_src = "25%沪深300全收益+75%H11023(买入持有)合成"
        # 对齐交易日，共同起点 rebase=1000
        ix = eq.index.intersection(syn_lvl.index)
        if len(ix) > 250:
            a2 = eq.reindex(ix)
            b2 = syn_lvl.reindex(ix).ffill()
            a2n = a2 / a2.iloc[0] * 1000.0
            b2n = b2 / b2.iloc[0] * 1000.0
            dev = (a2n / b2n - 1) * 100.0
            M["m3"] = {"data": pack(ix, a2n, b2n, dev),
                       "cur": {"hs300": lv(a2n)[0], "syn": lv(b2n)[0],
                               "ratio": lv(a2n / b2n)[0], "dev": lv(dev)[0],
                               "date": ix[-1].strftime("%Y-%m-%d")}}
            SRC["m3_bench"] = bench_src
            SRC["m3_eq"] = "沪深300全收益指数(H00300)"
            log(f"  M3 韭圈儿指标 OK（沪深300={SRC['m3_eq']}；基准={bench_src}；"
                f"{len(ix)} 日 {ix[0]:%Y-%m-%d}~{ix[-1]:%Y-%m-%d}）")

            # ---------- M3b：韭圈儿神奇指标 · 基金净值版（原著口径）----------
        # 原著「沪深300指数基金的累计涨幅」对比「偏债混合基金指数」，
        # 用易方达沪深300(110020)累计净值 + 万得偏债混合基金指数(885003.WI)重建，贴近 2022 年 4 月截图画面。
        _fe = fetch_fund_nav("110020")   # 易方达沪深300指数：沪深300指数基金代表
        _wd = load("wind885003")         # 万得偏债混合基金指数(885003.WI)
        if _fe is not None and _wd is not None and len(_fe) and len(_wd):
            _s300 = pd.Series(pd.to_numeric(_fe["nav"], errors="coerce").values,
                              index=pd.to_datetime(_fe["date"], format="%Y%m%d")).dropna().sort_index()
            _svdb = pd.Series(pd.to_numeric(_wd["close"], errors="coerce").values,
                              index=pd.to_datetime(_wd["date"], format="%Y-%m-%d")).dropna().sort_index()
            _jn = _s300.index.intersection(_svdb.index)
            if len(_jn) > 20:
                _a = _s300.reindex(_jn) / _s300.reindex(_jn).iloc[0] * 100.0
                _b = _svdb.reindex(_jn) / _svdb.reindex(_jn).iloc[0] * 100.0
                M["m3b"] = {"data": pack(_jn, _a, _b),
                            "cur": {"eq": lv(_a)[0], "dm": lv(_b)[0],
                                    "date": _jn[-1].strftime("%Y-%m-%d")}}
                SRC["m3b_funds"] = "易方达沪深300(110020) vs 偏债混合基金指数(885003)"
                log(f"  M3b 韭圈儿·基金净值版 OK（{len(_jn)} 日 {_jn[0]:%Y-%m-%d}~{_jn[-1]:%Y-%m-%d}）")

    # ---------- M4 偏股基金 3 年滚动年化 ----------
    if "930950" in S:
        s = S["930950"]
        r3 = roll_annual(s) * 100
        cols = [r3]
        try:
            zz = fetch_zzqz985()
            if zz is not None and len(zz):
                cols.append(zz.reindex(s.index).ffill())
        except Exception as e:
            log(f"  中证全指叠加失败: {e}")
        M["m4"] = {"data": pack(s.index, *cols),
                   "cur": {"r3": lv(r3)[0], "date": s.index[-1].strftime("%Y-%m-%d")}}
        log("  M4 偏股基金3年滚动年化 OK" + ("（含中证全指叠加）" if len(cols) > 1 else ""))

    # ---------- M5 偏股基金 vs 一级债基顶部共振（原文口径：2007 年末 = 1000） ----------
    # 出处：EarlETF 图表周刊(张翼轸)复刻雪球 @qzy69 的思路 —— 从 2008 年 A 股顶部(2007-12-31)起，
    # 把偏股基金指数与「只能参与打新(后改投可转债)的一级债基」累计收益相比，把一级债基的
    # 累计收益线当作偏股基金指数的「估算顶线」：2015-06 与 2021-02 两次大顶，偏股都涨到
    # 贴近(930950 口径 1.04/1.08)债基收益线的位置。偏股线用「中证偏股基金指数(930950)」，
    # 债基线用「万得一级债基指数(885006.WI)」(张翼轸原文以 Wind 一级债基指数替代易方达增强回报)。
    # ⚠️ 修正记录：此前版本曾把偏股腿换成 885001(万得偏股混合) 且基日取 2003-12-31，
    #   与原文图形差异巨大(2021 顶 885001 超债基 34% vs 原文"两次顶都只略高"不符)，2026-09-02 改回。
    def _m5_gauge(eq_s, bd_s, anchor="2007-12-31"):
        anc = pd.Timestamp(anchor)
        base = eq_s.index.intersection(bd_s.index)
        idx = base.union([anc]) if anc not in base else base
        # 先对含锚点的完整索引 reindex+ffill：锚点日无真实行情时向前补齐为基准行，
        # 使累计收益线严格从 anchor = 1000 起（原文约定 2007-12-31=1000）。
        a_full = eq_s.reindex(idx).ffill()
        b_full = bd_s.reindex(idx).ffill()
        m = idx <= anc
        if not m.any():
            return None
        i0 = idx[m][-1]          # 归一化基准：anchor 当天，或其前最近交易日
        sub = idx[idx >= i0]
        if len(sub) <= 250:
            return None
        a, b = a_full.reindex(sub), b_full.reindex(sub)
        na, nb = a / a.loc[i0] * 1000, b / b.loc[i0] * 1000
        ratio = na / nb
        lr = float(ratio.iloc[-1])
        gap = round((1.0 / lr - 1.0) * 100.0, 1) if lr < 1 else round(-(lr - 1.0) * 100.0, 1)
        return {"data": pack(sub, na, nb, ratio),
                "cur": {"eq": lv(na)[0], "bond": lv(nb)[0], "ratio": lv(ratio)[0],
                        "gap": gap, "rmax": round(float(ratio.max()), 3),
                        "rmaxd": ratio.idxmax().strftime("%Y-%m-%d"),
                        "rmin": round(float(ratio.min()), 3),
                        "rmind": ratio.idxmin().strftime("%Y-%m-%d"),
                        "date": sub[-1].strftime("%Y-%m-%d")}}

    _w_bd_p = os.path.join(CACHE, "wind885006.csv")
    _m5 = _m5_gauge(S["930950"], pd.read_csv(_w_bd_p, parse_dates=["date"]).set_index("date")["close"].sort_index()) \
        if "930950" in S and os.path.exists(_w_bd_p) else None
    if _m5:
        M["m5"] = _m5
        SRC["m5"] = "中证偏股基金指数(930950) vs 万得一级债基指数(885006.WI)，2007-12-31=1000"
        log(f"  M5 顶部共振 OK（原文口径：930950偏股基金 vs 885006一级债基，2007末锚定）")
    # 降级：Wind 一级债基缓存缺失 → 中证债券基金指数(H11023，同为一级债基代用)，锚点同上
    elif "930950" in S and "H11023" in S:
        _m5 = _m5_gauge(S["930950"], S["H11023"])
        if _m5:
            M["m5"] = _m5
            SRC["m5"] = "中证偏股基金指数(930950) vs 中证债券基金指数(H11023)，2007-12-31=1000（降级）"
            log("  M5 顶部共振 OK（原文口径，Wind 一级债基缺失 → H11023 降级）")

    # ---------- M5b 偏股基金 vs 易方达增强回报（@qzy69 原始一级债基口径） ----------
    # 与 M5 同一思路，但债基线用 @qzy69 原始使用的「易方达增强回报(110017)」累计净值，
    # 起点为该基金成立日 2008-03-19（也是 2008 顶部之后），是 qzy69 最初的原版测法。
    if "930950" in S and yf is not None and len(yf) > 250:
        _m5b = _m5_gauge(S["930950"], yf, anchor=yf.index[0].strftime("%Y-%m-%d"))
        if _m5b:
            M["m5b"] = _m5b
            log(f"  M5b 顶部共振(易方达增强回报) OK ({_m5b['data']['dates'][0]} 起)")

    # ---------- 轮动三棱镜 ----------
    def prism(ka, kb, la, lb, mid):
        if ka not in S or kb not in S:
            return
        a, b = S[ka], S[kb]
        idx = a.index.intersection(b.index)
        if len(idx) < 400:
            return
        a, b = a.reindex(idx).ffill(), b.reindex(idx).ffill()
        ratio = a / b
        midr, up, dn = boll(ratio, 252, 2)
        ma5 = ratio.rolling(1250).mean()
        d40 = diff_n(a, b, 40)
        d40ma = d40.rolling(252).mean()
        M[mid] = {"labels": [la, lb],
                  "ratio": pack(idx, ratio, midr, up, dn),
                  "ma5": pack(idx, ratio, ma5),
                  "d40": pack(idx, d40, d40ma),
                  "indices": pack(idx, a, b),
                  "cur": {"ratio": lv(ratio)[0], "boll_mid": lv(midr)[0], "boll_up": lv(up)[0],
                          "boll_dn": lv(dn)[0], "ma5": lv(ma5)[0], "d40": lv(d40)[0],
                          "d40ma": lv(d40ma)[0], "a": lv(a)[0], "b": lv(b)[0],
                          "date": idx[-1].strftime("%Y-%m-%d")}}
        log(f"  {mid} 三棱镜 OK")

    prism("H31589", "H31586", "300成长创新(全收益)", "300价值稳健(全收益)", "m6a")
    prism("H31591", "H31588", "1000成长创新(全收益)", "1000价值稳健(全收益)", "m6b")
    prism("000300", "000852", "沪深300", "中证1000", "m6c")
    prism("sz399370", "sz399371", "国证成长", "国证价值", "m7")

    # ---------- M8 中证红利股息率 ----------
    dy_zh = None
    if ind922 is not None and len(ind922):
        d = ind922.dropna(subset=["dy"])
        if len(d):
            dy_zh = pd.Series(d["dy"].values,
                              index=pd.to_datetime(d["date"], format="%Y%m%d")).sort_index()
    # 长周期：上证红利股息率 = 锚定派息率 ÷ 乐咕滚动市盈率
    dy_long = None
    if pe015 is not None and len(pe015) and ind015 is not None and len(ind015):
        try:
            d0 = ind015.dropna(subset=["dy", "pe"])
            if len(d0):
                payout = float(d0["dy"].iloc[0]) * float(d0["pe"].iloc[0]) / 100.0
                pe_s = pd.Series(pe015["v"].values,
                                 index=pd.to_datetime(pe015["date"], format="%Y%m%d")).sort_index()
                dy_long = payout / pe_s * 100.0
                log(f"    上证红利派息率锚定 = {payout:.3f}")
        except Exception as e:
            log(f"    上证红利股息率估算失败: {e}")
    if dy_zh is not None and len(dy_zh):
        rt = rates.copy()
        rt.index = pd.to_datetime(rt["date"], format="%Y%m%d")
        cn = rt["cn10y"].astype(float).reindex(dy_zh.index).ffill()
        spread = dy_zh - cn
        series, labels = [dy_zh, spread], ["中证红利股息率", "风险溢价(减10Y国债)"]
        if dy_long is not None:
            series.append(dy_long.reindex(dy_zh.index).ffill())
            labels.append("上证红利股息率(长周期估算)")
        M["m8"] = {"data": pack(dy_zh.index, *series), "labels": labels,
                   "cur": {"dy": lv(dy_zh)[0], "cn10y": lv(cn)[0], "spread": lv(spread)[0],
                           "dy_long": lv(dy_long)[0] if dy_long is not None else None,
                           "date": dy_zh.index[-1].strftime("%Y-%m-%d")}}
        log("  M8 红利股息率 OK（中证红利日频）")
    elif dy_long is not None and rates is not None and len(rates):
        # 降级：中证红利日频股息率暂不可得，改用上证红利反推的长周期估算
        rt = rates.copy()
        rt.index = pd.to_datetime(rt["date"], format="%Y%m%d")
        cn = rt["cn10y"].astype(float).reindex(dy_long.index).ffill()
        spread = dy_long - cn
        M["m8"] = {"data": pack(dy_long.index, dy_long, spread), "fb": True,
                   "labels": ["上证红利股息率(估算)", "风险溢价(减10Y国债)"],
                   "cur": {"dy": lv(dy_long)[0], "cn10y": lv(cn)[0], "spread": lv(spread)[0],
                           "dy_long": lv(dy_long)[0],
                           "date": dy_long.index[-1].strftime("%Y-%m-%d")}}
        log("  M8 红利股息率 OK（降级：上证红利估算）")

    # ---------- M9 红利 40 日收益差（vs 中证A股） ----------
    if "000922" in S and "930903" in S:
        a, b = S["000922"], S["930903"]
        idx = a.index.intersection(b.index)
        a, b = a.reindex(idx).ffill(), b.reindex(idx).ffill()
        d40, d40ma = diff_n(a, b, 40), None
        d40ma = d40.rolling(252).mean()
        M["m9"] = {"data": pack(idx, d40, d40ma),
                   "cur": {"d40": lv(d40)[0], "d40ma": lv(d40ma)[0],
                           "date": idx[-1].strftime("%Y-%m-%d")}}
        log("  M9 红利40日收益差 OK")

    # ---------- M10 中证红利回归曲线（价格指数版） & M10b（全收益版） ----------
    # 原文回归本应基于「中证红利全收益」，中证官网 H00922 即官方全收益。
    # M10 用价格指数 000922，M10b 用全收益 H00922，两版并列对比。
    def _clc_reg(skw):
        """对某只指数序列做 2016 年起的对数回归通道，返回 (s2) 即可。"""
        s = S.get(skw)
        if s is None:
            return None
        s2 = s[s.index >= "2016-01-01"]
        if len(s2) <= 300:
            return None
        x = np.arange(len(s2), dtype=float)
        y = np.log(s2.values.astype(float))
        c = np.polyfit(x, y, 1)
        fit = np.polyval(c, x)
        sd = float(np.std(y - fit, ddof=1))
        fv = pd.Series(np.exp(fit), index=s2.index)
        up = pd.Series(np.exp(fit + 1.5 * sd), index=s2.index)
        dn = pd.Series(np.exp(fit - 1.5 * sd), index=s2.index)
        z = (math.log(float(s2.iloc[-1])) - float(fit[-1])) / sd
        # 200日均线：必须在全史 s 上滚动（含2015年热身），再对齐到 s2，否则2016上半年MA是假值
        ma200 = s.rolling(200).mean().reindex(s2.index)
        return {"s2": s2, "fit": fit, "fv": fv, "up": up, "dn": dn, "z": z,
                "ma200": ma200,
                "ann": round((math.exp(c[0] * 250) - 1) * 100, 2),
                "date": s2.index[-1].strftime("%Y-%m-%d")}

    _r_price = _clc_reg("000922") if "000922" in S else None
    if _r_price:
        M["m10"] = {"data": pack(_r_price["s2"].index, _r_price["s2"], _r_price["fv"],
                                 _r_price["up"], _r_price["dn"], _r_price["ma200"]),
                    "cur": {"close": lv(_r_price["s2"])[0], "fit": lv(_r_price["fv"])[0],
                            "up": lv(_r_price["up"])[0], "dn": lv(_r_price["dn"])[0],
                            "ann": _r_price["ann"], "z": round(_r_price["z"], 2), "date": _r_price["date"]}}
        log("  M10 红利回归曲线 OK（价格指数 000922）")

    _r_tr = _clc_reg("H00922") if "H00922" in S else None
    if _r_tr:
        M["m10b"] = {"data": pack(_r_tr["s2"].index, _r_tr["s2"], _r_tr["fv"],
                                  _r_tr["up"], _r_tr["dn"], _r_tr["ma200"]),
                     "cur": {"close": lv(_r_tr["s2"])[0], "fit": lv(_r_tr["fv"])[0],
                             "up": lv(_r_tr["up"])[0], "dn": lv(_r_tr["dn"])[0],
                             "ann": _r_tr["ann"], "z": round(_r_tr["z"], 2), "date": _r_tr["date"]}}
        SRC["m10_tr"] = "H00922"
        log("  M10b 红利回归曲线 OK（全收益 H00922）")

    # ---------- M11 红利 A股 vs 港股 40 日收益差 ----------
    if "000922" in S and "930914" in S:
        a, b = S["000922"], S["930914"]
        idx = a.index.intersection(b.index)
        if len(idx) > 300:
            a, b = a.reindex(idx).ffill(), b.reindex(idx).ffill()
            d40, d40ma = diff_n(a, b, 40), None
            d40ma = d40.rolling(252).mean()
            M["m11"] = {"data": pack(idx, d40, d40ma),
                        "cur": {"d40": lv(d40)[0], "d40ma": lv(d40ma)[0],
                                "date": idx[-1].strftime("%Y-%m-%d")}}
            log("  M11 红利AH 40日收益差 OK")

    # ---------- M13 交易集中度（成交额排名前5%个股占全市场成交额比重） ----------
    # 历史序列由 tc_backfill.py（新浪成交量×收盘价≈成交额）回补至 cache/tc_concentration.csv；
    # 每日增量用腾讯实时快照(qt.gtimg.cn)真实成交额覆盖最新交易日。
    # 顶部K线：沪深300(000300) 作中证全A近似（中证全指000985的OHLC新浪仅到2016-06-13，不可靠）。
    # 沪深300 OHLC 走「实时拉取 → 失败则用本地缓存 cache/tc_hs300_ohlc.csv」容错，
    # 避免单次 Sina 超时导致整模块静默消失；若 OHLC 全缺则仅出比值面板（K线暂缺）。
    _tc = os.path.join(CACHE, "tc_concentration.csv")
    if os.path.exists(_tc):
        try:
            _tcdf = pd.read_csv(_tc, dtype={"date": str})
            _tcdf = _tcdf.dropna()
            _tcdf = _tcdf[_tcdf["ratio"] > 0]
            if len(_tcdf) < 60:
                log("  M13 交易集中度 跳过：历史序列过短")
            else:
                # 每日增量：腾讯实时快照真实成交额。
                # 若快照交易日 > 序列末日 → append 新行并写回 tc_concentration.csv（序列自动前进）；
                # 否则仅覆盖末日当日（重算核对）。股票池与历史回补池(tc_raw)一致。
                _codes = [os.path.splitext(f)[0] for f in os.listdir(RAW)
                          if f.endswith(".csv")] if os.path.isdir(RAW) else []
                _tc_last = str(_tcdf["date"].max())
                if _codes:
                    _t0 = time.time()
                    _amt = _tencent_amounts(_codes)
                    _t1 = time.time()
                    if _amt:
                        _tot = sum(_amt.values())
                        _arr = sorted(_amt.values(), reverse=True)
                        _k = max(1, int(round(len(_arr) * 0.05)))
                        _true = (sum(_arr[:_k]) / _tot * 100) if _tot > 0 else None
                        if _true is not None:
                            _snap, _snt = _tencent_snap(_codes)
                            # 完整交易日判定：快照时间>=15:00（避免盘中半天数据污染日/周/月序列）
                            _snap_ok = bool(_snt and _snt >= "150000")
                            if _snap and _snap > _tc_last and _snap_ok:
                                _tcdf = _tcdf.copy()
                                _tcdf.loc[len(_tcdf)] = [_snap, round(_true, 3)]
                                try:
                                    _tcdf.to_csv(_tc, index=False)
                                except Exception:
                                    pass
                                try:
                                    _tc_roll_maintain(_snap, _amt, _codes)
                                except Exception:
                                    pass
                            elif _snap and _snap == _tc_last and _snap_ok:
                                # 与序列末日同日且已收盘：真实成交额核对覆盖（如周六回看周五）
                                _tcdf = _tcdf.copy()
                                _tcdf.loc[_tcdf["date"] == _tc_last, "ratio"] = round(_true, 3)
                                # 同日也要重跑维护：缓冲区按日去重，可修复单位/窗口口径
                                try:
                                    _tc_roll_maintain(_snap, _amt, _codes)
                                except Exception:
                                    pass
                        log("  M13 明细：腾讯快照 %.1fs（%d 只）+ 缓冲/明细维护 %.1fs"
                            % (_t1 - _t0, len(_amt), time.time() - _t1))
                _ratio_map = {d: float(x) for d, x in zip(_tcdf["date"], _tcdf["ratio"])}

                # 顶部K线：中证全指(000985) 官方OHLC（csindex 缓存 + 每日增量刷新，非近似）
                _t2 = time.time()
                _oh = _csi_000985_ohlc()
                log("  M13 顶部OHLC(000985)刷新 %.1fs" % (time.time() - _t2))
                _oh_label = "中证全指(000985)"
                if not _oh:
                    log("  M13 交易集中度 警告：中证全指(000985) OHLC 缓存不可用，K线暂缺（仅出比值）")

                # 以中证全指交易日为轴（保证K线完整），比值按日期对齐
                _dates, _ratio, _ohlc = [], [], []
                for d, o, c, l, h in _oh:
                    if d in _ratio_map:
                        _dates.append(d)
                        _ratio.append(_ratio_map[d])
                        _ohlc.append([round(o, 2), round(c, 2), round(l, 2), round(h, 2)])
                if len(_dates) < 60:
                    # 兜底：OHLC 不可用，直接以比值序列为轴（不出 K 线）
                    _dates = [d for d in _tcdf["date"] if d >= DISPLAY_FROM]
                    _ratio = [_ratio_map[d] for d in _dates]
                    _ohlc = []
                    _oh_label = _oh_label + "（K线暂缺）"
                if len(_dates) > 60:
                    _k0 = bisect.bisect_left(_dates, DISPLAY_FROM)
                    if _k0 > 0:
                        _dates, _ratio, _ohlc = _dates[_k0:], _ratio[_k0:], _ohlc[_k0:]
                    _n = len(_dates)
                    if _n > 1800:
                        _step = (_n // 1800) + 1
                        _idx = list(range(0, _n, _step))
                        if _idx[-1] != _n - 1:
                            _idx.append(_n - 1)
                        _dates = [_dates[i] for i in _idx]
                        _ratio = [_ratio[i] for i in _idx]
                        _ohlc = [_ohlc[i] for i in _idx] if _ohlc else []
                    _last = _ratio[-1]
                    _pct = round(float((np.array(_ratio) <= _last).mean()) * 100, 1)
                    _m13 = {
                        "cur": {"date": _dates[-1], "ratio": _last, "pct": _pct,
                                "warn": 45.0, "idx_label": _oh_label,
                                "ohlc_ok": bool(_ohlc),
                                "method": "新浪成交量×收盘价估算(历史) + 腾讯实时实测(最新)"},
                        "dates": _dates, "ratio": _ratio, "ohlc": _ohlc}
                    # 周度 / 月度集中度（tc_freq.py 全史聚合；此处仅读，窗口最新点由 _tc_roll_maintain 维护）
                    for _fn, _fk in (("tc_concentration_weekly.csv", "weekly"),
                                     ("tc_concentration_monthly.csv", "monthly")):
                        _fp = os.path.join(CACHE, _fn)
                        if os.path.exists(_fp):
                            try:
                                _f = pd.read_csv(_fp, dtype={"date": str}).dropna()
                                _f = _f[_f["ratio"] > 0]
                                _da = [str(x) for x in _f["date"] if x >= DISPLAY_FROM]
                                _ra = [float(r) for d, r in zip(_f["date"], _f["ratio"])
                                       if d >= DISPLAY_FROM]
                                if len(_da) > 2 and len(_da) == len(_ra):
                                    _m13[_fk] = {"dates": _da, "ratio": _ra}
                            except Exception:
                                pass
                    M["m13"] = _m13
                    _wl = _m13.get("weekly", {}).get("ratio") or []
                    _ml = _m13.get("monthly", {}).get("ratio") or []
                    log(f"  M13 交易集中度 OK（{_n} 交易日，最新 {_last:.2f}%，"
                        f"历史分位 {_pct:.1f}%，K线{'有' if _ohlc else '缺'}，"
                        f"周度 {_wl[-1] if _wl else '-'}%，月度 {_ml[-1] if _ml else '-'}%）")
        except Exception as e:
            log(f"  M13 交易集中度 失败: {e}")

    # ---------- 降级说明 ----------
    fb = []
    for k, src in used.items():
        if not str(src[0]).startswith("csi") and k in FALLBACK_NOTE:
            fb.append({"m": k, "t": FALLBACK_NOTE[k][0], "d": FALLBACK_NOTE[k][1]})
    out["fellback"] = fb
    out["notes"] = build_notes(used, SRC)

    # 裁剪展示窗口（指标均已算完，此处只裁输出以减小体积）
    for k in list(M.keys()):
        for sub in ("data", "ratio", "ma5", "d40"):
            v = M[k].get(sub)
            if isinstance(v, dict) and "dates" in v:
                # m1 起点提前(五年均线热身)；m1b 更早(展示04-05年行情背景)；m5 起点=2007-12-31(原文锚点=1000)
                _from = {"m1": M1_DISPLAY_FROM, "m1b": M1B_DISPLAY_FROM,
                         "m5": "2007-12-31"}.get(k, DISPLAY_FROM)
                M[k][sub] = thin(trim(v, _from))
    # 信号箭头对齐到抽稀后的交易日：信号日若已被抽稀掉，则吸附到最近的不晚于它的交易日，
    # 并用该渲染日的价格作 y，保证箭头的 x(日期) 与 y(价格) 来自同一个渲染日、在图上精确落位。
    if "m1b" in M:
        _d0, _c0 = M["m1b"]["data"]["dates"], M["m1b"]["data"]["cols"][0]
        _lut = {d: i for i, d in enumerate(_d0)}
        def _snap(mark):
            date = mark[0]
            i = _lut.get(date, bisect.bisect_right(_d0, date) - 1)
            if i < 0:
                return None
            return [str(_d0[i]), _c0[i]]
        for _key in ("buys", "sells"):
            _sn = [x for x in (_snap(m) for m in M["m1b"]["marks"][_key]) if x]
            _seen, _dd = set(), []
            for x in _sn:                       # 抽稀后可能挤到同一交易日，去重保留首个
                if x[0] not in _seen:
                    _seen.add(x[0]); _dd.append(x)
            M["m1b"]["marks"][_key] = _dd
    # 用过的降级源提示
    if fb:
        out["notes"] = fb + out["notes"]
    out["sources"] = [
        {"k": "中证指数官网", "v": "全部中证指数日线，含 H 前缀全收益指数（官方）", "s": "ok"},
        {"k": "国证指数网", "v": "399370 / 399371 日线，2005 年至今（官方 hq.cnindex.com.cn）", "s": "ok"},
        {"k": "中债登", "v": "中债国债收益率曲线 10 年期（官方，按年下载 XLSX）", "s": "ok"},
        {"k": "美国财政部", "v": "Daily Treasury Par Yield Curve 10 Yr（官方，按年 CSV）", "s": "ok"},
        {"k": "乐咕乐股", "v": "沪深300 / 上证红利 PE、PB 历史（月频；官方仅免费提供近 20 日）", "s": "fb"},
        {"k": "腾讯行情", "v": "兜底源，仅当官方源失败时启用", "s": "fb"},
        {"k": "AkShare", "v": "国债收益率兜底，仅当官方源失败时启用", "s": "fb"},
        {"k": "Wind 万得", "v": "881001.WI 万得全A、885003.WI 偏债混合、885001.WI 偏股混合、885006.WI 混合债券型一级"
             "（官方 API 拉取全历史；手动低频执行，不纳入每日刷新）", "s": "ok"},
    ]

    # ---- 模块兜底：本轮未算出的模块，沿用上一份 data.json 的既有结果 ----
    # 目的：任何一轮抓取/计算失败都不允许让某个图卡整块消失（用户要求「不要没更新就不显示」）。
    # 上一轮结果即使略滞后，也比空白或占位文案有用；滞后情况会在日志里显式列出。
    _prev_path = os.path.join(BASE, "data.json")
    if os.path.exists(_prev_path):
        try:
            with open(_prev_path, "r", encoding="utf-8") as _f:
                _prev = json.load(_f)
            _pm = (_prev or {}).get("modules") or {}
            _carried = [_k for _k in _pm if _k not in M]
            for _k in _carried:
                M[_k] = _pm[_k]
            if _carried:
                out["carried"] = sorted(_carried)
                log("   模块兜底：沿用上一轮结果的模块 -> " + ", ".join(sorted(_carried)))
        except Exception as _e:
            log(f"   模块兜底：读取上一轮 data.json 失败（忽略）：{_e}")

    # ---- 数据截止日（asof）：供前端在「当日数据未更新」时显式标注「截至前一交易日」 ----
    # 用户要求：当日数据未更新时显示前一交易日的，不要出现空白/占位文案。
    # 前端据 asof 与 latest 的差异，在落后的卡片标题旁打「截至 MM-DD」标签；
    # 卡片本身照常用最近可得数据渲染，绝不因为当日缺失而清空。
    _asof = {}
    for _k, _v in M.items():
        _d = ""
        if isinstance(_v, dict):
            _c = _v.get("cur") or {}
            _d = str(_c.get("date") or "")
            if not _d:
                _dd = (_v.get("data") or {}).get("dates") or []
                if _dd:
                    _d = str(_dd[-1])
        _asof[_k] = _d
    out["asof"] = _asof
    _vals = sorted(v for v in _asof.values() if v)
    out["latest"] = _vals[-1] if _vals else ""
    _stale = {k: v for k, v in _asof.items() if v and out["latest"] and v < out["latest"]}
    out["stale"] = _stale
    if _stale:
        log("   数据截止：全站最新 " + out["latest"] + "；滞后卡片（沿用最近可得数据）-> "
            + ", ".join(f"{k}={v}" for k, v in sorted(_stale.items(), key=lambda x: x[1])))

    raw = json.dumps(out, ensure_ascii=False, separators=(",", ":"))
    _atomic_write(os.path.join(BASE, "data.json"), raw)
    _atomic_write(os.path.join(BASE, "data.js"), "window.DATA=" + raw + ";")
    log(f"=== 完成  data.json {os.path.getsize(os.path.join(BASE,'data.json'))//1024} KB ===")


def build_notes(used, SRC=None):
    SRC = SRC or {}
    m3_eq = "H00300" if "H00300" in used else "000300"
    n = [
        {"m": "官方源", "t": "国债收益率已改为官方直取",
         "d": "中国 10 年期国债收益率取自<b>中债登</b>（中债国债收益率曲线，按年下载官方 XLSX）；"
             "美国 10 年期取自<b>美国财政部</b>（Daily Treasury Par Yield Curve，官方 CSV）。"
             "两者均为发布方官网原始数据，仅在官方源失败时才降级到 AkShare。"},
        {"m": "官方源", "t": "中证指数全部走中证官网",
         "d": "包括 930xxx / H11xxx / 000xxx 系列日线，以及 H 前缀的<b>全收益指数</b>。"
             "上证红利(000015) 由中证指数公司编制，已由腾讯改为中证官网（腾讯降级为兜底）。"
             "国证成长/价值(399370/399371) 已改用<b>国证指数网官方行情接口</b>"
             "（hq.cnindex.com.cn），单次可取 2005 年至今全历史，腾讯降级为兜底。"},
        {"m": "官方源", "t": "目前仅 PE/PB 长周期历史仍用第三方",
         "d": "中证官网只免费提供最近约 20 个交易日的日频估值，长周期 PE/PB 无官方免费接口，"
             "故 M2 的市盈率与市赚率仍取自乐咕乐股（月频，2005 年至今）。"
             "这是本页<b>唯一</b>仍在使用的第三方数据源，且只影响 M2 的估值曲线。"},
        {"m": "M1", "t": "标的已换成正宗「万得全A(881001.WI)」",
         "d": "原文标题即 <b>「Wind全A五年之锚」</b>，正宗标的是万得全A指数(881001.WI)。"
             "此前因无公开日频源而退用中证A股(930903, 2005 起)，现已通过 <b>Wind 官方 API</b> 取到"
             " 1999-12-30 至今的真实全历史（与 930903 日收益相关系数 0.995，走势高度一致）。"
             "换来的是历史长度：五年均线提前到 2004-12、10 年分位曲线提前到 <b>2006-01</b>。"
             "注：仍为<b>价格指数</b>（万得全A全收益版无公开源），偏离度会比全收益口径系统性偏低约 2%/年，"
             "看趋势与相对高低不受影响。"},
        {"m": "M2", "t": "市赚率由真实 PB/PE 推导",
         "d": "市赚率 = PE ÷ (ROE×100)，其中 ROE = PB ÷ PE，PB 与 PE 均取自乐咕乐股（月频，2005 年至今）。"
             "沪深300 当前 ROE 约 11%。PE/PB 的长周期历史<b>无官方免费接口</b>，乐咕是业内通用的第三方源。"},
        {"m": "M2", "t": "股债性价比为月频",
         "d": "受限于免费的指数 PE 源，股债性价比曲线为月频（每月最后一个交易日）。国债收益率为日频，按交易日对齐后向前填充。"},
        {"m": "M3", "t": "万得偏债混合型基金指数(885003.WI) · Wind 真实数据",
         "d": "指标原话：<b>「沪深300指数基金的累计涨幅，跌至于偏债混合基金相若时，往往就是市场见底时」</b>——"
             "即把<b>沪深300全收益(H00300)</b>与<b>万得偏债混合型基金指数(885003.WI)</b>两条累计涨幅曲线同起点对照，两线交汇即为底部信号。"
             "红线使用中证官网官方全收益 H00300；橙线使用<b>Wind 真实 885003.WI</b>（经 Wind AImarket 拉取并缓存，"
             "<b>手动/低频更新、不纳入每日刷新</b>）。两线均 rebase 到 2004-12-31=1000。"},
        {"m": "M3", "t": f"沪深300 已用官方全收益 H00300（当前：{m3_eq}）",
         "d": "原文使用沪深300全收益，中证官网 <code>H00300</code> 即官方全收益指数，"
             "本模块已改用官方全收益，<b>不再有价格指数替代的偏差</b>。" if m3_eq == "H00300"
             else "官方全收益 H00300 本次未取到，暂用价格指数 000300，后续自动回补后切换。"},
        {"m": "M5", "t": "已还原原文口径：中证偏股基金(930950) vs Wind 一级债基(885006)，2007-12-31=1000",
         "d": "忠实复刻 EarlETF 原文（张翼轸复刻雪球 @qzy69）：<b>从 2008 年 A 股顶部(2007-12-31)起</b>，把偏股基金指数与<b>只能打新(后改投可转债)的一级债基</b>累计收益相比，<b>一级债基累计收益线(图中红线)＝偏股基金指数的估算顶线</b>——2015-06 与 2021-02 两次大顶，偏股都涨到贴近该线(比值 1.04 / 1.08)的位置。修正记录：此前误把偏股腿换成 885001(万得偏股混合)且基日取 2003-12-31，图形与原文差异巨大(885001 的 2021 顶超债基 34%，与原文「两次顶都只略高」不符)；2026-09-02 改回 <b>中证偏股基金指数(930950)</b>(即原文所指「偏股基金指数」) + <b>2007 年末锚点</b>。债基线用 <b>Wind 885006.WI</b>（张翼轸原文即用 Wind 一级债基指数替代易方达增强回报；缓存在缺失时降级 H11023）。<b>判读不看分位、看与红线(估算顶)的距离</b>：偏股贴近/上穿红线＝进入顶部估算区(2007 年以来上穿仅占 0.4% 交易日)；大幅低于红线(2024 年比值仅 0.51) 则离估算顶很远。"},
        {"m": "M5b", "t": "@qzy69 原始口径 → 易方达增强回报债券(110017)",
         "d": "与 M5 同源思路，但债券基准改用 @qzy69 原始使用的一级债基「易方达增强回报」(110017, 2008-03-19 成立)。因该基金 2008 年才成立，两条曲线改从两者首个共同交易日起归一化到 1000。一级债基含打新增强，真实累计收益高于普通纯债，是最贴近原文的本土口径。"},
        {"m": "M7", "t": "国证成长100/价值100 → 国证成长/国证价值",
         "d": "原文的国证成长100(980080) / 国证价值100(980081) 无公开历史行情接口（东财历史接口在本网络被封锁，同花顺 / 雪球均未覆盖）。改用同一编制机构（深证信息）的国证成长(399370) / 国证价值(399371)，风格含义一致。<b>国证指数官网未提供批量历史接口，当前走腾讯行情</b>。"},
        {"m": "M8", "t": "红利长周期股息率为反推估算",
         "d": "中证红利的日频股息率仅能取到最近约 20 个交易日（随每日运行累积）。长周期曲线用上证红利(000015)反推：派息率 = 当前股息率 × 当前市盈率（中证官方数据锚定），历史股息率 = 派息率 ÷ 乐咕滚动市盈率。这是估算，用于看长期分位，绝对值请以日后的短周期真实值为准。"},
        {"m": "M10 / M10b", "t": "M10 用价格指数 000922，M10b 用官方全收益 H00922",
         "d": "原文回归曲线本应基于中证红利全收益，中证官网 <code>H00922</code> 即官方全收益指数。"
             "本页把两版并列：<b>M10</b> 用<b>价格指数 000922</b>（对标传统 K 线点位）；"
             "<b>M10b</b> 用<b>官方全收益 H00922</b>（含分红再投，更贴合原文、不再低估约 2%/年）。"
             "全收益点位长期单调向上，通道用于看相对位置（Z 值）而非绝对高低。"},
        {"m": "M12", "t": "市盈率/市净率取自乐咕乐股（月频），ROE 由 PB÷PE 推算",
         "d": "本模块为 EarlETF《沪深300 估值一图看全》的复现：<b>沪深300全收益(H00300)走势 + 市盈率 TTM + 市赚率(PR=PE/ROE)</b>。"
             "走势用<b>中证官方全收益 H00300</b>；市盈率与市净率取自<b>乐咕乐股</b>（月频，2005 年至今），"
             "ROE 由 <code>PB ÷ PE</code> 推算，市赚率 <code>PR = PE ÷ (ROE×100)</code>。"
             "<b>诚实标注</b>：PE/PB 的长周期历史<b>无官方免费接口</b>，乐咕是业内通用第三方源；"
             "受限于此，本模块三张图均为<b>月频</b>，且 ROE 为 PB/PE 反推的近似值（非财报直接披露的 ROE）。"
             "历史分位按各自序列的全体历史样本计算。"},
    ]
    return n


if __name__ == "__main__":
    import sys
    main(deep=("--quick" not in sys.argv))
