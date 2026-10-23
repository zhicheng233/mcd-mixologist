#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
把 data/*.json 注入 templates/demo.template.html，生成 docs/index.html。

为什么要构建而不是手写：在线 Demo 的 JS 打分逻辑是 mix.py 的移植版，
如果餐品风味和心情向量在两边各写一份，改一边忘一边就会悄悄漂移。
这里让 JSON 成为唯一数据源，Demo 页面每次从它重新生成。

    python scripts/build_demo.py
"""

from __future__ import annotations

import io
import json
import os
import sys

for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:  # pragma: no cover
            pass

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PLACEHOLDER = "__DATA__"


def main() -> int:
    flavor = json.load(io.open(os.path.join(ROOT, "data", "flavor-db.json"), encoding="utf-8"))
    mood = json.load(io.open(os.path.join(ROOT, "data", "mood-db.json"), encoding="utf-8"))

    payload = {"flavor": flavor, "moods": mood["moods"]}
    blob = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    # 防止 JSON 里出现 </script> 把标签提前闭合
    blob = blob.replace("</", "<\\/")

    tpl_path = os.path.join(ROOT, "templates", "demo.template.html")
    tpl = io.open(tpl_path, encoding="utf-8").read()
    if PLACEHOLDER not in tpl:
        print("模板里找不到占位符 %s" % PLACEHOLDER, file=sys.stderr)
        return 1

    html = tpl.replace(PLACEHOLDER, blob)
    out_dir = os.path.join(ROOT, "docs")
    os.makedirs(out_dir, exist_ok=True)
    out_path = os.path.join(out_dir, "index.html")
    io.open(out_path, "w", encoding="utf-8").write(html)

    print("✓ docs/index.html 已生成")
    print("  餐品 %d 个 / 心情 %d 种 / %.1f KB" % (len(flavor["items"]), len(mood["moods"]), len(html) / 1024.0))
    return 0


if __name__ == "__main__":
    sys.exit(main())
