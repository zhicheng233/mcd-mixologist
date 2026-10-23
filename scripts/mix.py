#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
麦门调酒师 · 确定性配方引擎

设计原则（和本项目 README 里写的一致）：
    取数交给 MCP，配比交给代码，表达交给 Agent。

这个脚本只做一件事：把「心情 + 约束」翻译成「餐品组合」。
它不联网、不依赖任何第三方库、不做任何主观发挥，同样的输入永远得到同样的输出。
Agent 负责听懂用户说什么、以及在配方出来后把话说好听。

用法：
    python scripts/mix.py --mood tired --budget 30
    python scripts/mix.py --text "今天加班到十点，想喝点甜的，预算30"
    python scripts/mix.py --mood irritable --exclude dairy --max-kcal 900 --json
    python scripts/mix.py --list-moods
    python scripts/mix.py --selftest
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import sys
from typing import Any, Dict, List, Optional

# Windows 控制台默认 GBK，直接 print emoji / 中文会炸，这里统一成 UTF-8。
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:  # pragma: no cover
            pass

DIMS = ["sweet", "heat", "umami", "fresh", "rich", "warm"]

# 一杯的结构权重：基酒决定这一杯的底色，辅料给厚度，调和负责收口。
SLOT_WEIGHTS = {"base": 0.55, "side": 0.25, "mixer": 0.20}

# 忌口命中一次的扣分
AVOID_PENALTY = 0.35
# 预算超出比例的上限，超过直接淘汰
BUDGET_HARD_RATIO = 1.45

# 相似度校准：归一化欧氏距离算出来的原始值集中在 0.85~0.98，
# 直接当百分比显示会全是「99%」，没有信息量。
# 上界取 1.10 是因为满分 = 风味 1.0 + 结构分 0.07 + 预算分 0.03。
SIM_FLOOR = 0.55
SIM_CEIL = 1.10

# 用户嘴上说的偏好，优先级高于心情推断出来的目标
CRAVE_MAP = {
    "甜": {"sweet": 0.35},
    "辣": {"heat": 0.35},
    "清爽": {"fresh": 0.35, "rich": -0.15},
    "清淡": {"fresh": 0.30, "rich": -0.25, "heat": -0.20},
    "浓": {"rich": 0.30},
    "热": {"warm": 0.30},
    "冰": {"warm": -0.35},
    "饱": {"rich": 0.25, "umami": 0.15},
}

MEAL_LABELS = {
    "breakfast": "早餐",
    "lunch": "午餐",
    "dinner": "晚餐",
    "late-night": "夜宵",
}

# 心情笔记是按时段无关写的，但"今晚""过夜"这种词放在早餐卡片上很出戏。
# 早餐时按整句替换，比给 8 种心情各写一套笔记更好维护。
BREAKFAST_TEXT = {
    "热的、厚的、不用动脑的，就是今晚最对的配方。": "热的、厚的、不用动脑的，就是今早最对的配方。",
    "碳水兜底，别空着肚子过夜，明天还要接着跑。": "碳水兜底，别空着肚子出门，上午还有一堆事。",
    "别加沙拉，今晚不需要清爽，需要分量。": "别加沙拉，早上不需要清爽，需要分量。",
    "避开油炸和咖啡因，今晚不需要再被推一把。": "避开油炸和咖啡因，上午不需要再被推一把。",
}

# 餐段自带的回避项。午餐和晚餐的菜单确实一样，但夜宵不该再推提神的。
MEAL_AVOID = {
    "late-night": (
        ["caffeine"],
        "夜宵这顿别再上咖啡因了，不然躺下就是两小时后的事。",
    ),
}


# ---------------------------------------------------------------- 基础工具


def _root() -> str:
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def load_json(*parts: str) -> Dict[str, Any]:
    path = os.path.join(_root(), *parts)
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def cosine(a: Dict[str, float], b: Dict[str, float]) -> float:
    """风味相似度。

    这里不能用余弦：风味向量的每一维都是 0~1 的非负数，余弦在任何两个
    「都挺浓」的向量之间都会逼近 1，8 种心情会给出同一杯。改用归一化欧氏
    距离，才能真的把「辣 vs 甜」「冰 vs 热」区分开。
    """
    d2 = sum((a.get(d, 0.0) - b.get(d, 0.0)) ** 2 for d in DIMS)
    return 1.0 - math.sqrt(d2) / math.sqrt(len(DIMS))


def blend(items: List[Dict[str, Any]]) -> Dict[str, float]:
    """把一杯里各个部件的风味按结构权重加权，得到整杯的风味画像。"""
    total_w = 0.0
    acc = {d: 0.0 for d in DIMS}
    for item, slot in items:
        w = SLOT_WEIGHTS[slot]
        total_w += w
        for d in DIMS:
            acc[d] += item["flavor"].get(d, 0.0) * w
    if total_w == 0.0:
        return acc
    return {d: round(acc[d] / total_w, 4) for d in DIMS}


# ---------------------------------------------------------------- 心情解析


def resolve_mood(mood_db: Dict[str, Any], query: str) -> Optional[Dict[str, Any]]:
    """用 id / 名称 / 别名做匹配。别名越长越优先，避免'累'吃掉'累到没力气'。"""
    q = (query or "").strip().lower()
    if not q:
        return None
    best: Optional[Dict[str, Any]] = None
    best_len = -1
    for mood in mood_db["moods"]:
        keys = [mood["id"], mood["name"]] + list(mood.get("aliases", []))
        for k in keys:
            k = k.lower()
            if k and k in q and len(k) > best_len:
                best, best_len = mood, len(k)
    return best


def apply_craving(target: Dict[str, float], text: str) -> Dict[str, float]:
    """把「想喝点甜的」这类显式偏好叠到心情目标上。"""
    out = dict(target)
    for word, delta in CRAVE_MAP.items():
        if word in text:
            for d, v in delta.items():
                out[d] = max(0.0, min(1.0, out.get(d, 0.0) + v))
    return out


def detect_meal(text: str) -> Optional[str]:
    if any(w in text for w in ("早餐", "早饭", "早上")):
        return "breakfast"
    if any(w in text for w in ("午餐", "午饭", "中午")):
        return "lunch"
    if any(w in text for w in ("夜宵", "宵夜", "深夜", "凌晨")):
        return "late-night"
    if any(w in text for w in ("晚餐", "晚饭", "晚上", "今晚", "下班")):
        return "dinner"
    return None


def calibrate(sim: float) -> float:
    return max(0.0, min(1.0, (sim - SIM_FLOOR) / (SIM_CEIL - SIM_FLOOR)))


def parse_text(text: str) -> Dict[str, Any]:
    """从一句自然语言里抽出引擎需要的字段。只做保守提取，抽不到就留空。"""
    out: Dict[str, Any] = {
        "mood": None,
        "budget": None,
        "max_kcal": None,
        "exclude": [],
        "meal": detect_meal(text),
    }

    m = re.search(r"(?:预算|不超过|最多|就|只有)\s*[¥￥]?\s*(\d+(?:\.\d+)?)", text)
    if not m:
        m = re.search(r"[¥￥]\s*(\d+(?:\.\d+)?)", text)
    if m:
        out["budget"] = float(m.group(1))

    m = re.search(r"(\d{3,4})\s*(?:大卡|kcal|卡)", text, re.I)
    if m:
        out["max_kcal"] = int(m.group(1))

    exclude_words = {
        "beef": ["不吃牛", "牛肉不行", "不要牛", "忌牛"],
        "chicken": ["不吃鸡", "不要鸡"],
        "fish": ["不吃鱼", "不要鱼"],
        "pork": ["不吃猪", "不要猪"],
        "dairy": ["乳糖", "不吃奶", "不要芝士", "不吃芝士"],
        "gluten": ["麸质", "不吃面包"],
        "caffeine": ["不要咖啡因", "不喝咖啡", "失眠"],
        "fried": ["不吃油炸", "不要炸"],
        "sugar": ["无糖", "不要糖"],
    }
    for tag, words in exclude_words.items():
        if any(w in text for w in words):
            out["exclude"].append(tag)

    return out


# ---------------------------------------------------------------- 配方计算


def build_candidates(db: Dict[str, Any], meal: Optional[str]) -> Dict[str, List[Dict[str, Any]]]:
    buckets: Dict[str, List[Dict[str, Any]]] = {"base": [], "side": [], "mixer": []}
    for item in db["items"]:
        period = item.get("period", "all")
        # period 三态：
        #   breakfast —— 只在早餐时段供应（麦满分、薯饼、油条、豆浆）
        #   main      —— 10:30 之后才供应（汉堡、薯条、炸鸡、冰淇淋）
        #   all       —— 全天供应（可乐、雪碧、咖啡等饮品）
        if period == "breakfast" and meal != "breakfast":
            continue
        if period == "main" and meal == "breakfast":
            continue
        role = item["role"]
        if role == "dessert":
            buckets["side"].append(item)
        elif role in buckets:
            buckets[role].append(item)
    return buckets


def score_combo(
    slots: Dict[str, Dict[str, Any]],
    mood: Dict[str, Any],
    req: Dict[str, Any],
) -> Optional[Dict[str, Any]]:
    target = mood["target"]
    ordered = [(slots[s], s) for s in ("base", "side", "mixer") if s in slots]
    flavor = blend(ordered)
    match = cosine(flavor, target)

    price = round(sum(it["price"] for it, _ in ordered), 2)
    kcal = sum(it["kcal"] for it, _ in ordered)

    warnings: List[str] = []
    score = match

    # 结构分：完整的一杯应该有辅料和调和。预算紧张时这项自然会被超预算扣分压掉。
    if "side" in slots and "mixer" in slots:
        score += 0.07
    elif "side" in slots:
        score += 0.045
    elif "mixer" in slots:
        score += 0.025

    # 甜品当正餐辅料是降级用法，只有甜味需求明显时才该顶上来
    if slots.get("side", {}).get("role") == "dessert":
        score -= 0.05

    # 忌口：硬过滤，不参与排序
    all_tags = set()
    for it, _ in ordered:
        all_tags.update(it.get("contains", []))
    for ex in req.get("exclude") or []:
        if ex in all_tags:
            return None

    # 心情自带的回避项：不淘汰，但明确扣分并给出理由
    hit_avoid = [a for a in mood.get("avoid", []) if a in all_tags]
    if hit_avoid:
        score -= AVOID_PENALTY * len(hit_avoid)
        warnings.append(mood.get("avoid_reason") or "配方里有这杯不该出现的东西。")

    # 温度一致性：想喝热的却给了一杯冰的，是最容易翻车的地方
    want_warm = target.get("warm", 0.5)
    if want_warm >= 0.75 and flavor["warm"] < 0.30:
        score -= 0.22
        warnings.append("这杯偏凉，和你要的温度不太对。")
    elif want_warm <= 0.35 and flavor["warm"] > 0.80:
        score -= 0.10

    # 预算：先硬卡上限，卡不住才退到宽松档
    budget = req.get("budget")
    cap = req.get("budget_cap")
    if cap is not None and price > cap:
        return None
    if budget:
        if price > budget:
            over = price - budget
            score -= min(0.30, 0.5 * over / max(budget, 1.0))
            warnings.append("超预算 ¥%.1f，需要你点头才做。" % over)
        elif price >= budget * 0.55:
            score += 0.03  # 用足预算的配方通常更完整，轻微倾斜

    # 热量上限
    max_kcal = req.get("max_kcal")
    if max_kcal:
        if kcal > max_kcal:
            score -= 0.25
            warnings.append("热量超出你给的上限 %d kcal。" % (kcal - max_kcal))

    return {
        "slots": slots,
        "flavor": flavor,
        "match": round(calibrate(match), 4),
        "fit": round(calibrate(score), 4),
        "score": round(score, 4),
        "price": price,
        "kcal": kcal,
        "warnings": warnings,
    }


def suggest_missing(
    recipe: Dict[str, Any],
    buckets: Dict[str, List[Dict[str, Any]]],
    mood: Dict[str, Any],
    req: Dict[str, Any],
    avoid_ids: Optional[set] = None,
) -> Optional[Dict[str, Any]]:
    """配方缺辅料或调和时，补一条「建议加配」。

    建议不参与主排序、不套预算硬约束，只按风味匹配挑最合的一件，
    并如实标出多加多少钱。
    """
    slots = dict(recipe["slots"])
    relax = dict(req)
    # 加配是可选动作，不受原预算硬卡；但也不该给一杯贵到离谱的，
    # 所以放宽到 1.35 倍，超了就在卡片上如实标出来。
    budget = req.get("budget")
    relax["budget"] = None
    relax["budget_cap"] = (budget * 1.35) if budget else None
    avoid_ids = avoid_ids or set()

    best = None
    if "mixer" not in slots:
        pool = [(m, "mixer") for m in buckets["mixer"]]
    elif "side" not in slots:
        pool = [(s, "side") for s in buckets["side"]]
    else:
        pool = []

    for item, slot in pool:
        if item["id"] in avoid_ids:
            continue
        # 建议是「额外加一件」，没有任何补偿理由去推一个卡片上刚说要避开的东西。
        # 主配方里回避项是扣分项（用户非要点也行），但建议必须干净。
        avoid_tags = mood.get("avoid") or []
        if any(t in (item.get("contains") or []) for t in avoid_tags):
            continue
        trial = dict(slots)
        trial[slot] = item
        scored = score_combo(trial, mood, relax)
        if scored and (best is None or scored["score"] > best[0]["score"]):
            best = (scored, slot, item)

    if not best:
        return None
    scored, slot, item = best
    total = round(recipe["price"] + item["price"], 2)
    return {
        "slot": slot,
        "label": "调和" if slot == "mixer" else "辅料",
        "item": item,
        "match": scored["match"],
        "delta_price": round(item["price"], 2),
        "delta_kcal": item["kcal"],
        "total_price": total,
        "over_budget": bool(budget and total > budget),
    }


def mix(req: Dict[str, Any], top_n: int = 3) -> Dict[str, Any]:
    db = load_json("data", "flavor-db.json")
    mood_db = load_json("data", "mood-db.json")

    mood_query = req.get("mood") or req.get("text") or ""
    mood = resolve_mood(mood_db, mood_query)
    if mood is None:
        raise SystemExit(
            "没听懂今天是什么心情。用 --mood 指定，或 --list-moods 看全部选项。"
        )

    text = req.get("text") or ""
    if text:
        parsed = parse_text(text)
        for k in ("budget", "max_kcal", "meal"):
            if req.get(k) is None and parsed[k] is not None:
                req[k] = parsed[k]
        if not req.get("exclude"):
            req["exclude"] = parsed["exclude"]

    # 心情给底色，用户嘴上说的偏好盖在上面
    mood = dict(mood)
    if text:
        mood["target"] = apply_craving(mood["target"], text)

    # 餐段自带的回避项（夜宵不推咖啡因），与心情回避项合并
    meal_rule = MEAL_AVOID.get(req.get("meal") or "")
    if meal_rule:
        extra, reason = meal_rule
        merged = list(mood.get("avoid") or [])
        added = False
        for a in extra:
            if a not in merged:
                merged.append(a)
                added = True
        mood["avoid"] = merged
        if added and not mood.get("avoid_reason"):
            mood["avoid_reason"] = reason

    buckets = build_candidates(db, req.get("meal"))

    def collect(eff_req: Dict[str, Any]) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        for base in buckets["base"]:
            for side in [None] + buckets["side"]:
                for mixer in [None] + buckets["mixer"]:
                    slots = {"base": base}
                    if side:
                        slots["side"] = side
                    if mixer:
                        slots["mixer"] = mixer
                    scored = score_combo(slots, mood, eff_req)
                    if scored:
                        out.append(scored)
        return out

    # 第一轮：严格按用户给的预算卡死。调不出来再放宽，并且明确标出超了多少。
    strict = dict(req)
    if req.get("budget"):
        strict["budget_cap"] = req["budget"]
    results = collect(strict)
    if not results and req.get("budget"):
        loose = dict(req)
        loose["budget_cap"] = req["budget"] * BUDGET_HARD_RATIO
        results = collect(loose)

    # 排序用「适配度」而不是纯风味相似度：卡片上显示的数字必须和排列顺序一致，
    # 否则用户会看到第一名 84%、第二名 89%，然后开始怀疑这个工具。
    results.sort(key=lambda r: (r["fit"], r["score"]), reverse=True)

    # 多样性：同一个基酒、同一份辅料都只出现一次，避免三杯是同一杯的变体
    picked: List[Dict[str, Any]] = []
    seen_base = set()
    seen_side = set()
    for r in results:
        bid = r["slots"]["base"]["id"]
        sid = r["slots"].get("side", {}).get("id")
        if bid in seen_base or (sid and sid in seen_side):
            continue
        seen_base.add(bid)
        if sid:
            seen_side.add(sid)
        picked.append(r)
        if len(picked) >= top_n:
            break

    recipes = []
    used_suggestions: set = set()
    is_breakfast = req.get("meal") == "breakfast"
    for i, r in enumerate(picked):
        names = mood.get("names") or [mood["name"]]
        notes = mood.get("notes") or [""]
        note = notes[i % len(notes)]
        opener = (mood.get("openers") or [""])[i % len(mood.get("openers") or [""])]
        if is_breakfast:
            note = BREAKFAST_TEXT.get(note, note)
            opener = BREAKFAST_TEXT.get(opener, opener)
        recipe = {
            "name": names[i % len(names)],
            "mood": mood["id"],
            "mood_name": mood["name"],
            "opener": opener,
            "slots": r["slots"],
            "flavor": r["flavor"],
            "match": r["match"],
            "fit": r["fit"],
            "price": r["price"],
            "kcal": r["kcal"],
            "note": note,
            "warnings": r["warnings"],
            "suggestion": None,
        }
        # 缺哪个部件就补一条建议，保证卡片上永远是完整的三段式
        sug = suggest_missing(recipe, buckets, mood, req, used_suggestions)
        if sug:
            used_suggestions.add(sug["item"]["id"])
            recipe["suggestion"] = sug
        recipes.append(recipe)

    return {
        "engine": "mcd-mixologist/1",
        "mood": {"id": mood["id"], "name": mood["name"]},
        "meal": req.get("meal"),
        "meal_label": MEAL_LABELS.get(req.get("meal") or "", "随时"),
        "request": {
            "budget": req.get("budget"),
            "max_kcal": req.get("max_kcal"),
            "exclude": req.get("exclude") or [],
        },
        "recipes": recipes,
        "caveat": "价格与热量为离线演示快照，正式展示前必须用 calculate-price / list-nutrition-foods 复核。",
    }


# ---------------------------------------------------------------- 输出


BAR = "▮▮▮▮▮▯▯▯▯▯"


def bar(v: float) -> str:
    n = max(0, min(5, int(round(v * 5))))
    return "▮" * n + "▯" * (5 - n)


def render(payload: Dict[str, Any]) -> str:
    lines: List[str] = []
    mood_name = payload["mood"]["name"]
    lines.append("🍸 今日特调 · %s" % mood_name)
    lines.append("")

    for r in payload["recipes"]:
        lines.append("── %s ──" % r["name"])
        if r.get("opener"):
            lines.append("   %s" % r["opener"])
        for slot, label in (("base", "基酒"), ("side", "辅料"), ("mixer", "调和")):
            it = r["slots"].get(slot)
            if not it:
                continue
            lines.append("   %s　%s　%s" % (label, it["name"], it["note"]))
        f = r["flavor"]
        lines.append(
            "   风味　辣 %s　浓郁 %s　清爽 %s"
            % (bar(f["heat"]), bar(f["rich"]), bar(f["fresh"]))
        )
        lines.append("   配比　适配度 %.0f%%　¥%.1f　%d kcal" % (r["fit"] * 100, r["price"], r["kcal"]))
        sug = r.get("suggestion")
        if sug:
            lines.append(
                "   建议加配%s　%s　+¥%.1f（合计 ¥%.1f%s）"
                % (
                    sug["label"],
                    sug["item"]["name"],
                    sug["delta_price"],
                    sug["total_price"],
                    "，超预算" if sug["over_budget"] else "",
                )
            )
        for w in r["warnings"]:
            lines.append("   ⚠ %s" % w)
        lines.append("   调酒师笔记：%s" % r["note"])
        lines.append("")

    lines.append("（%s）" % payload["caveat"])
    return "\n".join(lines)


# ---------------------------------------------------------------- 自测


def selftest() -> int:
    db = load_json("data", "flavor-db.json")
    mood_db = load_json("data", "mood-db.json")
    failures: List[str] = []

    def check(cond: bool, msg: str) -> None:
        if not cond:
            failures.append(msg)

    # 数据完整性
    ids = [i["id"] for i in db["items"]]
    check(len(ids) == len(set(ids)), "餐品 id 有重复")
    for item in db["items"]:
        for d in DIMS:
            v = item["flavor"].get(d)
            check(isinstance(v, (int, float)) and 0.0 <= v <= 1.0, "%s 的 %s 越界" % (item["id"], d))
        check(item["role"] in ("base", "side", "mixer", "dessert"), "%s 的 role 非法" % item["id"])

    check(len(mood_db["moods"]) >= 6, "心情少于 6 种")
    for m in mood_db["moods"]:
        for d in DIMS:
            check(0.0 <= m["target"].get(d, -1) <= 1.0, "%s 的目标向量越界" % m["id"])

    # 确定性：同输入同输出
    a = mix({"mood": "tired", "budget": 30})
    b = mix({"mood": "tired", "budget": 30})
    check(json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True), "引擎不是确定性的")

    # 预算约束
    r = mix({"mood": "treat", "budget": 20})
    check(all(x["price"] <= 20 * BUDGET_HARD_RATIO for x in r["recipes"]), "预算硬约束失效")

    # 忌口硬过滤
    r = mix({"mood": "empty", "exclude": ["beef"]})
    for x in r["recipes"]:
        for it in x["slots"].values():
            check("beef" not in it.get("contains", []), "忌口过滤失效: %s" % it["name"])

    # 心情区分度：疲惫与亢奋的首杯不该是同一个基酒
    t = mix({"mood": "tired"})["recipes"][0]["slots"]["base"]["id"]
    h = mix({"mood": "high"})["recipes"][0]["slots"]["base"]["id"]
    check(t != h, "不同心情给出了一样的首杯（%s）" % t)

    # 文本解析
    p = parse_text("今天加班到十点，想喝点甜的，预算30，不要咖啡因")
    check(p["budget"] == 30.0, "预算解析失败")
    check("caffeine" in p["exclude"], "忌口解析失败")
    check(resolve_mood(mood_db, "今天加班到十点")["id"] == "tired", "心情识别失败")

    # 每杯必须有基酒
    for m in mood_db["moods"]:
        r = mix({"mood": m["id"]})
        check(len(r["recipes"]) == 3, "%s 没有给出 3 杯" % m["id"])
        for x in r["recipes"]:
            check("base" in x["slots"], "%s 有一杯没有基酒" % m["id"])

    if failures:
        print("✗ 自测失败 %d 项：" % len(failures))
        for f in failures:
            print("  - %s" % f)
        return 1

    print("✓ 自测通过：%d 个餐品 / %d 种心情" % (len(db["items"]), len(mood_db["moods"])))
    return 0


# ---------------------------------------------------------------- CLI


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="麦门调酒师 · 确定性配方引擎")
    ap.add_argument("--mood", help="心情 id 或中文名，如 tired / 疲惫")
    ap.add_argument("--text", help="一句话描述，引擎会自己抽心情、预算、忌口")
    ap.add_argument("--budget", type=float, help="预算上限（元）")
    ap.add_argument(
        "--meal",
        choices=["breakfast", "lunch", "dinner", "late-night"],
        help="餐段，默认按 text 推断；不给则不限（早餐品会被排除）",
    )
    ap.add_argument("--max-kcal", type=int, dest="max_kcal", help="热量上限（kcal）")
    ap.add_argument("--exclude", action="append", default=[], help="忌口标签，可重复")
    ap.add_argument("--top", type=int, default=3, help="出几杯，默认 3")
    ap.add_argument("--json", action="store_true", help="输出 JSON，供 Agent 二次加工")
    ap.add_argument("--list-moods", action="store_true", help="列出全部心情")
    ap.add_argument("--selftest", action="store_true", help="跑自测")
    args = ap.parse_args(argv)

    if args.selftest:
        return selftest()

    if args.list_moods:
        mood_db = load_json("data", "mood-db.json")
        for m in mood_db["moods"]:
            print("%-10s %-4s  %s" % (m["id"], m["name"], "、".join(m["aliases"][:4])))
        return 0

    if not args.mood and not args.text:
        ap.print_help()
        return 2

    req = {
        "mood": args.mood,
        "text": args.text,
        "budget": args.budget,
        "max_kcal": args.max_kcal,
        "exclude": args.exclude,
        "meal": args.meal,
    }
    payload = mix(req, top_n=args.top)
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(render(payload))
    return 0


if __name__ == "__main__":
    sys.exit(main())
