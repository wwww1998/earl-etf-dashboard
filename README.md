# EarlETF 图表周刊 · 指标看板（每日自动更新）

复现张翼轸《EarlETF 图表周刊 2026-08-30》中的 11 个图表模型，数据每日自动更新。

## 文件

| 文件 | 作用 |
|---|---|
| `index.html` | **构建产物**：自包含单文件看板（ECharts + 数据已内联，双击即可打开，离线可用） |
| `template.html` | 页面源码。**要改页面改这里**，改完跑 `build.py` |
| `build.py` | 把 `template.html` + `echarts.min.js` + `data.js` 打包成自包含的 `index.html` |
| `fetch_data.py` | 数据抓取 + 指标计算，输出 `data.json` / `data.js` |
| `backfill.py` | 中证历史补洞守护进程（应对官网 WAF 限流） |
| `probe.py` | 中证官网解封探针 |
| `update.cmd` | 每日更新入口：`fetch_data.py` → `build.py`，供定时任务调用 |
| `cache/` | 本地数据缓存（CSV，断点续传，勿删） |
| `echarts.min.js` | 本地 ECharts 源文件 |

> 为什么 `index.html` 是自包含的：预览器和部分静态服务器只会加载 `index.html` 本身，
> 同目录的 `data.js` / `echarts.min.js` 不会一起带上，页面会报「未加载到数据」。
> 打包成单文件后，`file://` 双击、预览器、任意静态目录都能正常打开。

## 手动更新

```bash
C:\Users\wxk11\.workbuddy\binaries\python\envs\default\Scripts\python.exe fetch_data.py
C:\Users\wxk11\.workbuddy\binaries\python\envs\default\Scripts\python.exe build.py
```

只做增量、不回补历史（日常快跑）：

```bash
python fetch_data.py --quick && python build.py
```

## 数据源

| 用途 | 来源 | 说明 |
|---|---|---|
| 中证系指数日线（930xxx / H11xxx） | 中证指数官网 `perf/index-perf` | 分段回补 + 增量；有 WAF 限流，脚本内置退避 |
| 沪深 / 深证系指数日线 | 腾讯行情 | 单次上限 2000 根（约 8 年） |
| 沪深300 / 上证红利 的 PE、PB | 乐咕乐股（AkShare） | 月频，2005 年至今 |
| 沪深300 / 中证红利 / 上证红利 的日频 PE 与股息率 | 中证官网静态 XLS | 最近约 20 个交易日，每日累积 |
| 中 / 美 10 年期国债收益率 | AkShare `bond_zh_us_rate` | 日频，全历史 |

## 已知口径替代

中证官网只提供价格指数，且部分标的无公开日频源。页面「口径与替代说明」一栏逐条披露，要点：

- **M1 / M3 / M10**：原文用全收益指数，本页用价格指数 → 收益与偏离度系统性偏低约每年 2%（股息）。
- **M3**：万得偏债混合型基金指数（Wind 专有）→ 用 `25% 沪深300 + 75% 中证债券基金指数` 合成。
- **M5**：Wind 一级债基指数 → 中证债券基金指数(H11023)，基日同为 2007-12-31。
- **M7**：国证成长100 / 价值100（980080 / 980081）无公开历史源 → 改用国证成长(399370) / 国证价值(399371)。
- **M8**：中证红利日频股息率仅 20 天（每日累积）；长周期曲线用上证红利按「派息率 ÷ 滚动市盈率」反推，属估算。
- **M2**：受免费 PE 源限制，股债性价比为月频。

## 定时任务

已配置自动化：每交易日 18:30 执行 `update.cmd`，刷新 `data.js` 后页面自动展示新数据。
