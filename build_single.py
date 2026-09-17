# -*- coding: utf-8 -*-
"""把 data.js 内联进 lance55.html，产出可双击 / 可发手机的单文件看板。

用法：
    python build_single.py                # 只产出 看板.html
    python build_single.py index.html     # 额外产出 index.html（GitHub Pages 用）
"""
import io
import json
import os
import sys

BASE = os.path.dirname(os.path.abspath(__file__))
html = io.open(os.path.join(BASE, "lance55.html"), "r", encoding="utf-8").read()

# 优先用 data.js；没有就从 data.json 现场拼一份（换目录/CI 首次构建都不会崩）
_dj, _dn = os.path.join(BASE, "data.js"), os.path.join(BASE, "data.json")
if os.path.exists(_dj):
    data = io.open(_dj, "r", encoding="utf-8").read()
elif os.path.exists(_dn):
    # 重新紧凑序列化，别把 data.json 的缩进带进页面（白涨 5KB+）
    _obj = json.load(io.open(_dn, "r", encoding="utf-8"))
    data = "window.__DATA__=" + json.dumps(_obj, ensure_ascii=False, separators=(",", ":")) + ";"
else:
    raise SystemExit("找不到 data.js / data.json，先跑 lance55_scan.py")

if '<script src="data.js"></script>' not in html:
    raise SystemExit("lance55.html 里找不到 data.js 引用，无法内联")
out = html.replace('<script src="data.js"></script>', "<script>\n" + data + "\n</script>")

targets = [os.path.join(BASE, "lance55_看板.html")]
for extra in sys.argv[1:]:
    targets.append(extra if os.path.isabs(extra) else os.path.join(BASE, extra))

for t in targets:
    io.open(t, "w", encoding="utf-8").write(out)
    print("OK ->", t, os.path.getsize(t), "bytes")
