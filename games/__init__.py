# -*- coding: utf-8 -*-
"""游戏注册表：新增一款棋 = 在 games/ 下建目录 + 在这里加一行。"""
from .shanhe import SPEC as SHANHE
from .shijie import SPEC as SHIJIE

GAMES = [SHANHE, SHIJIE]

# 大厅展示顺序（未列出的分类按出现顺序排在后面）
CATEGORY_ORDER = ['棋类', '牌类', '益智']


def categories():
    """按分类聚合，供大厅渲染。"""
    order = {c: i for i, c in enumerate(CATEGORY_ORDER)}
    buckets = {}
    for spec in GAMES:
        buckets.setdefault(spec.category, []).append(spec)
    out = []
    for cat in sorted(buckets, key=lambda c: order.get(c, 99)):
        out.append({'name': cat, 'games': [g.to_json() for g in buckets[cat]]})
    return out
