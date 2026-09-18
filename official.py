# -*- coding: utf-8 -*-
"""
官方数据源：中债登国债收益率曲线、美国财政部国债收益率。

中债登（中国）：https://yield.chinabond.com.cn  中债国债收益率曲线(到期)
  按年下载 XLSX，列 = 日期 / 标准期限说明 / 标准期限(年) / 收益率(%)
  筛选 标准期限(年)==10.0 即为 10 年期国债到期收益率。
  ycDefId（中债国债收益率曲线）= 2c9081e50a2f9606010a3068cae70001

美国财政部：https://home.treasury.gov  Daily Treasury Par Yield Curve Rates
  按年下载 CSV，列含 "10 Yr"，日期格式 MM/DD/YYYY。
"""
import os, io, time
import requests
import pandas as pd

BASE = os.path.dirname(os.path.abspath(__file__))
CACHE = os.path.join(BASE, "cache")
os.makedirs(CACHE, exist_ok=True)

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36")

# 中债国债收益率曲线(到期)
YCDEFID_CN_10Y = "2c9081e50a2f9606010a3068cae70001"
CHINABOND_URL = "https://yield.chinabond.com.cn/cbweb-mn/yc/downYearBzqx"
UST_URL = ("https://home.treasury.gov/resource-center/data-chart-center/interest-rates/"
           "daily-treasury-rates.csv/{yr}/all?type=daily_treasury_yield_curve"
           "&field_tdr_date_value={yr}&page&_format=csv")


def _sess():
    s = requests.Session()
    s.headers.update({"User-Agent": UA, "Accept-Language": "zh-CN,zh;q=0.9"})
    return s


def _cache_path(name):
    return os.path.join(CACHE, name)


# ---------------------------------------------------------------- 国证指数（官方）
# hq.cnindex.com.cn 是国证指数网(www.cnindex.com.cn)的官方行情子域，
# 单次可返回 2005 年至今的全部日线，无 WAF 限流 —— 远优于第三方源的 2000 根上限。
CNINDEX_URL = "http://hq.cnindex.com.cn/market/market/getIndexDailyDataWithDataFormat"


def cnindex_daily(code, start="2005-01-01", end=None, tries=3):
    """国证/深证系指数日线。返回 DataFrame[date(YYYYMMDD), close] 或 None"""
    import datetime as dt
    end = end or dt.date.today().strftime("%Y-%m-%d")
    s = _sess()
    for i in range(tries):
        try:
            r = s.get(CNINDEX_URL,
                      params={"indexCode": code, "startDate": start,
                              "endDate": end, "frequency": "day"},
                      timeout=60, headers={"Referer": "http://www.cnindex.com.cn/"})
            if r.status_code == 200:
                rows = ((r.json() or {}).get("data") or {}).get("data") or []
                if not rows:
                    return None
                rec = []
                for x in rows:
                    # 行格式: [日期, 收盘, 最高, 开盘, 最低, 收盘, 涨跌额, 涨跌幅, 成交额, 成交量, -]
                    if not isinstance(x, (list, tuple)) or len(x) < 6:
                        continue
                    try:
                        c = float(x[5])
                    except Exception:
                        continue
                    if c != c or not x[0]:
                        continue
                    rec.append((str(x[0]).replace("-", ""), c))
                if rec:
                    return pd.DataFrame(rec, columns=["date", "close"]) \
                             .drop_duplicates("date", keep="last") \
                             .sort_values("date").reset_index(drop=True)
            time.sleep(3)
        except Exception:
            time.sleep(3)
    return None


# ---------------------------------------------------------------- 中债登（中国 10Y）
def chinabond_year(year, sess=None, tries=3):
    """下载某一年的国债收益率曲线，返回 DataFrame[date(YYYYMMDD), cn10y]"""
    s = sess or _sess()
    for i in range(tries):
        try:
            r = s.get(CHINABOND_URL,
                      params={"year": str(year), "wrjxCBFlag": "0", "zblx": "",
                              "ycDefId": YCDEFID_CN_10Y, "locale": "zh_CN"},
                      timeout=60,
                      headers={"Referer": "https://yield.chinabond.com.cn/"})
            if r.status_code == 200 and len(r.content) > 2000 and r.content[:2] == b"PK":
                df = pd.read_excel(io.BytesIO(r.content))
                col = "标准期限(年)"
                if "日期" not in df.columns or col not in df.columns:
                    return None
                d = df[pd.to_numeric(df[col], errors="coerce") == 10.0].copy()
                if d.empty:
                    return None
                d["date"] = pd.to_datetime(d["日期"]).dt.strftime("%Y%m%d")
                d["cn10y"] = pd.to_numeric(d["收益率(%)"], errors="coerce")
                out = d[["date", "cn10y"]].dropna().drop_duplicates("date")
                return out.reset_index(drop=True)
            time.sleep(3)
        except Exception:
            time.sleep(3)
    return None


def chinabond_10y(y0=2006, y1=None, log=print, sleep=1.2):
    """多年合并，带按年 CSV 缓存。返回 DataFrame[date, cn10y] 或 None"""
    import datetime as dt
    y1 = y1 or dt.date.today().year
    sess = _sess()
    frames = []
    for y in range(y0, y1 + 1):
        ck = _cache_path(f"cb10y_{y}.csv")
        if os.path.exists(ck):
            try:
                d = pd.read_csv(ck, dtype={"date": str})
                if len(d):
                    frames.append(d)
                    continue
            except Exception:
                pass
        d = chinabond_year(y, sess)
        if d is None or not len(d):
            log(f"    中债登 {y} 年获取失败")
            time.sleep(sleep)
            continue
        try:
            d.to_csv(ck, index=False)
        except Exception:
            pass
        log(f"    中债登 {y}: {len(d)} 行")
        frames.append(d)
        time.sleep(sleep)
    if not frames:
        return None
    out = pd.concat(frames, ignore_index=True)
    out["date"] = out["date"].astype(str)
    return out.drop_duplicates("date", keep="last").sort_values("date").reset_index(drop=True)


# ---------------------------------------------------------------- 美国财政部（美国 10Y）
def ust_year(year, sess=None, tries=3):
    """下载某一年的美债收益率曲线，返回 DataFrame[date(YYYYMMDD), us10y]"""
    s = sess or _sess()
    for i in range(tries):
        try:
            r = s.get(UST_URL.format(yr=year), timeout=60, headers={"Accept": "text/csv"})
            if r.status_code == 200 and len(r.content) > 500:
                df = pd.read_csv(io.StringIO(r.text))
                col = None
                for c in df.columns:
                    if str(c).strip().lower() in ("10 yr", "10-yr", "10 yr."):
                        col = c
                        break
                if col is None or "Date" not in df.columns:
                    return None
                d = pd.DataFrame({
                    "date": pd.to_datetime(df["Date"], format="%m/%d/%Y",
                                           errors="coerce").dt.strftime("%Y%m%d"),
                    "us10y": pd.to_numeric(df[col], errors="coerce"),
                })
                return d.dropna().drop_duplicates("date").reset_index(drop=True)
            time.sleep(3)
        except Exception:
            time.sleep(3)
    return None


def ust_10y(y0=2006, y1=None, log=print, sleep=1.0):
    import datetime as dt
    y1 = y1 or dt.date.today().year
    sess = _sess()
    frames = []
    for y in range(y0, y1 + 1):
        ck = _cache_path(f"us10y_{y}.csv")
        if os.path.exists(ck):
            try:
                d = pd.read_csv(ck, dtype={"date": str})
                if len(d):
                    frames.append(d)
                    continue
            except Exception:
                pass
        d = ust_year(y, sess)
        if d is None or not len(d):
            log(f"    美财政部 {y} 年获取失败")
            time.sleep(sleep)
            continue
        try:
            d.to_csv(ck, index=False)
        except Exception:
            pass
        log(f"    美财政部 {y}: {len(d)} 行")
        frames.append(d)
        time.sleep(sleep)
    if not frames:
        return None
    out = pd.concat(frames, ignore_index=True)
    out["date"] = out["date"].astype(str)
    return out.drop_duplicates("date", keep="last").sort_values("date").reset_index(drop=True)
