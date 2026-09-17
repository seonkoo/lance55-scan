# -*- coding: utf-8 -*-
"""CI 保险：数据不合格就中止，绝不把残缺快照推上线。

判定（任一不满足即退出码 1）：
  1. 全市场列表 >= 4000 只（拿不到列表时 universe=0，必须拦住）
  2. 有效K线覆盖 >= 全市场的 90%
  3. 命中总数 > 0（真的跑出信号，不是静默 0）

上线策略是「宁可停在上一版，也不发一版错的」。
"""
import json
import os
import sys

BASE = os.path.dirname(os.path.abspath(__file__))
with open(os.path.join(BASE, "data.json"), "r", encoding="utf-8") as f:
    d = json.load(f)

meta, stats = d.get("meta", {}), d.get("stats", {})
uni = meta.get("universe", 0) or 0
scanned = stats.get("scanned", 0) or 0
hits = sum(len(d.get(k, [])) for k in ("buy_today", "buy_recent", "watch"))

print("universe=%d scanned=%d cover=%.1f%% hits=%d trade_date=%s" %
      (uni, scanned, scanned / max(1, uni) * 100, hits, meta.get("trade_date")))

errs = []
if uni < 4000:
    errs.append("全A列表只拿到 %d 只（应 ~5200），数据源异常" % uni)
if scanned < uni * 0.9:
    errs.append("日线覆盖 %d/%d = %.1f%%，低于 90%%" % (scanned, uni, scanned / max(1, uni) * 100))
if hits == 0:
    errs.append("0 命中（可能是静默异常），拒绝上线")

if errs:
    print("\n!! 数据校验未通过，本次不提交、不更新线上：")
    for e in errs:
        print("   -", e)
    sys.exit(1)
print("数据校验通过")
