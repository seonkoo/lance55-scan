# 55战法扫盘 · 兰斯洛t

A股全市场扫描：**5/10/20 多头排列 → 放量大阳突破日 55 线 → 突破后 1~3 天内回踩 55 线**。
每个交易日收盘后自动跑，结果渲染成单文件看板（手机上直接看）。

> 战法来源：NGA 帖 `tid=47288722` 中 UID `67142257`（兰斯洛t / 兰神）的「55 线介入战法」。
> 这是他个人交易方法的**机械化复现**，不是投资建议。他自称的胜率未经独立验证。

## 线上看板

`https://seonkoo.github.io/lance55-scan/`

（首次需在仓库 Settings → Pages → Source 选 `Deploy from a branch` → `main` / `/ (root)`，只做一次）

## 它怎么工作

```
GitHub Actions（每交易日 15:20 北京时间）
   └─ python lance55_scan.py   全A列表 → 腾讯前复权日线 → 信号 → data.json / data.js
   └─ python guard.py          数据不合格就中止（宁停上一版，不发错的）
   └─ python build_single.py index.html   把 data.js 内联成单文件
   └─ git commit && git push   提交 index.html + data.json
GitHub Pages 服务 index.html → 手机打开即最新
```

## 文件

| 文件 | 作用 |
|---|---|
| `lance55_scan.py` | **唯一引擎**。零第三方依赖，只用标准库 |
| `lance55_config.json` | 全部阈值。改策略只动这里，不碰 Python |
| `lance55.html` | 页面模板（读 `data.js`，本地 file:// 双击可看） |
| `build_single.py` | 把数据内联进 HTML，产单文件看板 |
| `guard.py` | CI 保险：数据不合格就拒绝上线 |
| `.github/workflows/daily.yml` | 定时任务定义 |
| `index.html` | **构建产物**，Pages 直接服务它 |
| `data.json` | 构建产物，原始信号数据 |

## 本地跑

```bash
python lance55_scan.py --limit 300    # 冒烟测试（只扫 300 只，不写缓存）
python lance55_scan.py                # 全量（约 8 分钟；当天二次运行走缓存，秒级）
python build_single.py lance55_看板.html
```

## 数据源与坑

- 日线：腾讯前复权（`web.ifzq.gtimg.cn`，主域名被 WAF 拦时自动降级到 `proxy.finance.qq.com`）
- 全A列表 / 行业：新浪（**`num` 上限 100**，传 500 只回 100 只；偶发空页，必须"连续两页空"才停）
- **限速 12 req/s + 4 并发是安全线**。实测 6 并发打到 ~50 req/s → 腾讯 WAF 封 IP（501），持续数分钟。
- `hq.sinajs.cn` 必须带 `Referer: https://finance.sina.com.cn`，否则 403。

## 已知边界（诚实标注）

- 行业分类覆盖约 **60%**（新浪行业板块接口只到这些），其余标"未分类"，不是数据缺失。
- 「A 类 / B 类」由日线近似：日线看不到分时速度，A 类仅表示**收盘收回 55 线上**。
- C 类 = 收盘落在 55 线下且**放量**，是他原则里的减仓/离场信号，别当买点用。
- 高振幅（≥15%）、一字板、今日涨停的会单独标注——要么是妖股，要么买不到。
