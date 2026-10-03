# -*- coding: utf-8 -*-
"""通用"旅游棋"机器人 AI（地图由房间的 room.M 注入，因此与具体棋盘解耦）。

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


def _affordable(room, pid, to_city):
    """这一步是否买得起，返回该路线或 None。"""
    p = room.players[pid]
    for r in room.neighbors(p['position']):
        if r['to'] != to_city:
            continue
        if p['tickets'][r['color']] + p['tickets'][room.M.WILD] >= r['cost']:
            return r
    return None


def targets_for(room, pid):
    """目标城市 -> 权重。

    关键：秘密目的地只有【分数达标时停留】才算获胜，所以分数不够时不能一直往
    那儿跑（否则会在目的地附近来回乒乓，永远凑不够分）。分数越接近目标分，
    目的地的权重才越高。
    """
    p = room.players[pid]
    tg = {}
    for c in room.shared_cards:
        if c:
            tg[c] = 1.0 + room.M.CITY_POINTS.get(c, 1) * 0.45   # 分值越高越吸引人
    secret = p.get('secret')
    if secret and not p.get('goal_done') and secret != p['position']:
        need = room.win_score - p['score']
        if need <= 0:
            add = 2.6        # 分数已达标：立刻冲目的地
        elif need <= 2:
            add = 1.6        # 快达标了：开始往目的地靠拢
        else:
            add = 0.4        # 分数还差很多：先专心收城市卡
        tg[secret] = tg.get(secret, 0.0) + add
    return tg


def next_step(room, pid, level='normal'):
    """返回下一步要去的城市名；None 表示本回合不再走。"""
    M = room.M
    p = room.players[pid]
    here = p['position']
    tg = targets_for(room, pid)
    # 已经站在公共卡城市上：停下即可拿分，别走开
    if here in room.shared_cards:
        return None
    if not tg:
        # 桌面上没有城市卡（牌堆抽完且都没命中）：也别站着不动，随便挪一步
        wander = [(r['cost'], r['to']) for r in room.neighbors(here)
                  if _affordable(room, pid, r['to'])]
        return min(wander)[1] if wander else None

    # 挑性价比最高的目标：权重高、距离近
    best = None
    for city, w in tg.items():
        d = M.ALL_DIST[here].get(city)
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
    for m, _color, _cost in M.ADJ[here]:
        if prev and m == prev and m != target:
            continue
        if not _affordable(room, pid, m):
            continue
        nd = M.ALL_DIST[m].get(target, 99)
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
    # 没有能缩短距离的走法（比如通往目的地的那一步买不起）：也绝不原地不动，
    # 否则会一直攒票、整局卡死。挑最便宜的一步挪过去，绕路或游走都行。
    options.sort(key=lambda x: (x[1], -x[0]))
    return options[0][2]
