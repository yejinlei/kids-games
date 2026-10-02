# -*- coding: utf-8 -*-
"""《山河之旅》机器人 AI。

策略核心（启发式，不做穷举）：
1. 目标评估：公共城市卡（分值越高越想要）+ 自己的秘密目的地（必须到达，权重最高）。
2. 每走一步前，用"忽略颜色的最短段数"估算到各目标的距离，挑性价比最高的目标。
3. 在当前位置的相邻城市中，选一个"买得起且能缩短到目标距离"的城市走一步；
   若能一步踏上目标城市就立刻踩上去（因为只有"最后停留的城市"才能拿卡）。
4. 站在公共卡城市上或没有能缩短距离的走法时，结束本次移动（不浪费车票）。

难度：
- normal：总是选最优走法，能直接踩卡就踩。
- easy  ：35% 概率随机走一步，偶尔错过最优解，适合儿童。
"""
import random
from collections import deque

from map_data import CITY_GEO, CITY_POINTS, ROUTES, WILD

# 忽略颜色的邻接表：(邻居, 颜色, 票数)
_ADJ = {c: [] for c in CITY_GEO}
for _a, _b, _color, _cost in ROUTES:
    _ADJ[_a].append((_b, _color, _cost))
    _ADJ[_b].append((_a, _color, _cost))


def _bfs(start):
    """从 start 出发，忽略颜色的最少段数距离。"""
    dist = {start: 0}
    q = deque([start])
    while q:
        n = q.popleft()
        for m, _c, _w in _ADJ[n]:
            if m not in dist:
                dist[m] = dist[n] + 1
                q.append(m)
    return dist


# 45 座城市，启动时一次性算好全源最短路，之后查表即可
ALL_DIST = {c: _bfs(c) for c in CITY_GEO}


def _affordable(room, pid, to_city):
    """这一步是否买得起，返回该路线或 None。"""
    p = room.players[pid]
    for r in room.neighbors(p['position']):
        if r['to'] != to_city:
            continue
        if p['tickets'][r['color']] + p['tickets'][WILD] >= r['cost']:
            return r
    return None


def targets_for(room, pid):
    """目标城市 -> 权重。"""
    p = room.players[pid]
    tg = {}
    for c in room.shared_cards:
        if c:
            tg[c] = 1.0 + CITY_POINTS.get(c, 1) * 0.45   # 分值越高越吸引人
    if p['secret'] and not p['goal_done']:
        tg[p['secret']] = tg.get(p['secret'], 0.0) + 2.2  # 目的地是获胜前提
    return tg


def next_step(room, pid, level='normal'):
    """返回下一步要去的城市名；None 表示本回合不再走。"""
    p = room.players[pid]
    here = p['position']
    tg = targets_for(room, pid)
    if not tg:
        return None
    # 已经站在公共卡城市上：停下即可拿分，别走开
    if here in room.shared_cards:
        return None

    # 挑性价比最高的目标：权重高、距离近
    best = None
    for city, w in tg.items():
        d = ALL_DIST[here].get(city)
        if not d:
            continue
        s = w / (d ** 1.4)
        if best is None or s > best[0]:
            best = (s, city, d)
    if not best:
        return None
    _, target, cur_d = best

    options = []
    # 不走回头路（除非那一步正好踩上目标），防止在两个城市间来回晃
    prev = room.move_path[-1] if len(room.move_path) >= 2 else None
    for m, _color, _cost in _ADJ[here]:
        if prev and m == prev and m != target:
            continue
        if not _affordable(room, pid, m):
            continue
        nd = ALL_DIST[m].get(target, 99)
        options.append((cur_d - nd, _cost, m))   # 缩短的距离、花费、城市
    if not options:
        return None

    # 能直接踩上目标城市就踩（拿卡 / 达成目的地）
    for gain, _cost, m in options:
        if m == target:
            return m

    if level == 'easy' and len(options) > 1 and random.random() < 0.35:
        return random.choice(options)[2]

    options.sort(key=lambda x: (-x[0], x[1]))    # 优先缩短距离，其次少花票
    gain, _cost, m = options[0]
    if gain > 0:
        return m
    # 没有能缩短距离的走法时：走一步最便宜的顺路步，保持推进（避免一直攒票不动）
    if gain == 0:
        return m
    # 距离反而变远但只要 1 张票：偶尔游走探路，避免看起来一直不动
    if _cost == 1 and random.random() < 0.4:
        return m
    return None
