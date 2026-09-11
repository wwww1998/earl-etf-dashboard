# -*- coding: utf-8 -*-
"""
中证历史补洞守护进程。
中证官网有 WAF 限流（约每窗口 20 次请求，之后封锁 10~30 分钟）。
本脚本在后台轮询，一旦解封就继续回补缺失的指数历史，直到补全或超时。
用法: python backfill.py [hours]
"""
import time, sys, datetime as dt
import fetch_data as fd

# 含新增的官方全收益指数 H00300 / H00922 与改走中证官网的 000015
CODES = ["H00300", "H00922", "000015",
         "930950", "H11023", "931591", "931588", "930914",
         "931589", "931586", "930903", "000300", "000922", "000852"]
MIN_ROWS = 1800

hours = float(sys.argv[1]) if len(sys.argv) > 1 else 2.0
deadline = time.time() + 3600 * hours


def log(m):
    print(f"[{dt.datetime.now():%H:%M:%S}] {m}", flush=True)


def pending():
    out = []
    for c in CODES:
        df = fd.load(f"csi{c}")
        n = 0 if df is None else len(df)
        if n < MIN_ROWS:
            out.append((c, n))
    return out


if __name__ == "__main__":
    log(f"补洞守护启动，最长 {hours} 小时")
    while time.time() < deadline:
        p = pending()
        if not p:
            log("全部指数历史已补全 ✓")
            break
        log(f"待补: {p}")
        fd.CSI_BLOCKED[0] = False
        fd.CSI_BLOCKED[1] = 0
        blocked = False
        for c, n in p:
            if time.time() > deadline:
                break
            fd.fetch_csi(c, deep=True)
            if fd.CSI_BLOCKED[0]:
                log("检测到限流，休眠 8 分钟后再试")
                blocked = True
                break
            time.sleep(18)
        if blocked:
            time.sleep(480)
        else:
            time.sleep(45)
    left = pending()
    log(f"退出。仍待补: {left if left else '无'}")
