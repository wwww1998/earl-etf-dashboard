# -*- coding: utf-8 -*-
"""
把 template.html + echarts.min.js + data.js 打包成一个自包含的 index.html。

为什么需要这一步：预览器 / 部分静态服务器只会加载 index.html 本身，
同目录的 data.js、echarts.min.js 不会一起带上，页面会报「未加载到数据」。
打包成单文件后，file:// 双击、预览器、任意静态目录都能正常打开。
"""
import os, datetime as dt

BASE = os.path.dirname(os.path.abspath(__file__))
p = lambda f: os.path.join(BASE, f)

ECHARTS_TAG = "<!--ECHARTS_HERE-->"
DATA_ANCHOR = "<!--DATA_HERE-->"


def read(f):
    with open(p(f), encoding="utf-8") as fh:
        return fh.read()


def main():
    tpl = read("template.html")
    ec = read("echarts.min.js")
    data = read("data.js")

    if ECHARTS_TAG not in tpl:
        raise SystemExit("template.html 中找不到 echarts 引用标签")
    if DATA_ANCHOR not in tpl:
        raise SystemExit("template.html 中找不到数据锚点")

    out = tpl.replace(ECHARTS_TAG, "<script>\n/* ECharts 5.5.1 (inlined) */\n" + ec + "\n</script>")
    out = out.replace(DATA_ANCHOR, "<script>\n/* 指标数据 (inlined) */\n" + data + "\n</script>\n\n")

    stamp = dt.datetime.now().strftime("%Y-%m-%d %H:%M")
    out = out.replace("</head>", f"<!-- 自包含单文件，打包于 {stamp} -->\n</head>")
    out = out.replace("<!--BUILD-->", "")

    with open(p("index.html"), "w", encoding="utf-8") as fh:
        fh.write(out)
    print(f"已生成自包含 index.html  {os.path.getsize(p('index.html'))//1024} KB  ({stamp})")


if __name__ == "__main__":
    main()
