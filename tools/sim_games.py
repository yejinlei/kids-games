# -*- coding: utf-8 -*-
"""专家团模拟：多配置批量对局，逐步校验规则不变量，发现异常即记录。"""
import os
import random
import collections
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import games.shijie.map_data as W
import games.shanhe.map_data as S
import core.journey as J
import core.ai as AI


def pids_all(room):
    return [q for q in room.order if q in room.players]


class Violation(Exception):
    pass


def tickets_total(tk):
    return sum(tk.values())


def cost_to(room, pid, to):
    for r in room.neighbors(room.players[pid]['position']):
        if r['to'] == to:
            return r['cost'], r['color']
    return None, None


def check_player(room, pid, ctx):
    p = room.players[pid]
    for k, v in p['tickets'].items():
        if v < 0:
            raise Violation('%s 车票为负 %s=%s (%s)' % (pid, k, v, ctx))
    if p['position'] not in room.M.CITY_GEO:
        raise Violation('%s 位置非法 %s (%s)' % (pid, p['position'], ctx))
    if p['score'] < ctx.get('score', 0):
        raise Violation('%s 分数倒退 %s->%s' % (pid, ctx.get('score'), p['score']))


def simulate(spec, seed):
    random.seed(seed)
    room = J.JourneyRoom(spec['mod'].build_map(), 'SIM',
                         max_players=spec['n'], steps_per_turn=spec['steps'],
                         win_score=spec['win'], ai_level=spec['ai'])
    pids = ['p%d' % i for i in range(spec['n'])]
    for i, pid in enumerate(pids):
        room.join(pid, '玩家%d' % i, 's%d' % i)
    room.start(room.host_pid)
    if spec.get('thin_deck'):          # 强制牌堆提前耗尽，测试桌面卡补不上的分支
        room.deck = room.deck[:1]

    expect = {pid: 0 for pid in pids}          # 手牌总数应有的值（掷骰+2、答题+1、移动-cost）
    rolls = {pid: 0 for pid in pids}
    stuck = {pid: 0 for pid in pids}
    card_owners = collections.Counter()        # 城市卡是否被两人各拿一次
    stats = {'turns': 0, 'moves': 0, 'quiz': 0, 'skipped': 0, 'auto': 0, 'max_stuck': 0}
    prev_score = {pid: 0 for pid in pids}

    for step in range(6000):
        if room.state != 'playing':
            break
        pid = room.current_pid
        if pid not in pids:
            raise Violation('回合指向未知玩家 %s' % pid)
        p = room.players[pid]
        ctx = {'score': prev_score[pid]}

        # ---- 断线模拟：随机让某个非当前玩家掉线 ----
        if spec.get('drop') and random.random() < 0.01:
            others = [q for q in pids if q != pid and room.players[q]['connected']]
            if len(others) > 1:
                room.mark_disconnected(random.choice(others))
                stats.setdefault('drop', 0)
                stats['drop'] += 1
        if not room.players[pid]['connected']:
            raise Violation('回合交给了已掉线的玩家 %s' % pid)

        # ---- 超时自动行动（覆盖 auto_play 分支）----
        if spec['auto'] and random.random() < 0.05:
            before = {q: tickets_total(room.players[q]['tickets']) for q in pids}
            room.auto_play(room.turn_token)
            for q in pids:      # 超时自动行动会掷骰+自动走一步，按实际变化记账
                expect[q] += tickets_total(room.players[q]['tickets']) - before[q]
            stats['auto'] += 1
            continue

        # ---- 掷骰 ----
        if room.phase == 'roll':
            before = tickets_total(p['tickets'])
            room.roll(pid)
            after = tickets_total(p['tickets'])
            if after - before != room.dice_count:
                raise Violation('%s 掷骰得票 %d≠%d' % (pid, after - before, room.dice_count))
            expect[pid] += room.dice_count
            rolls[pid] += 1
            stats['turns'] += 1

        # ---- 知识题（覆盖答题分支）----
        if p.get('quiz') and random.random() < spec['quiz_rate']:
            info = room.M.CITY_INFO.get(p['quiz']['city'], {})
            right = info.get('a', 0)
            n = len(info.get('o') or [])
            ok_rate = 0.5 if spec['ai'] == 'easy' else 0.75
            idx = right if (n and random.random() < ok_rate) else (
                (right + 1) % n if n > 1 else 0)
            before = tickets_total(p['tickets'])
            res = room.answer_quiz(pid, idx)
            after = tickets_total(p['tickets'])
            if res['ok'] and after - before != 1:
                raise Violation('%s 答对未得 GO（+%d）' % (pid, after - before))
            if not res['ok'] and after != before:
                raise Violation('%s 答错却得票（+%d）' % (pid, after - before))
            if res['ok']:
                expect[pid] += 1
            stats['quiz'] += 1

        # ---- 移动 ----
        moved_any = False
        guard = 0
        while room.state == 'playing' and room.phase == 'move' and room.steps_left > 0:
            guard += 1
            if guard > 10:
                raise Violation('同一回合移动步数失控')
            # 偶尔"不走直接结束回合"，覆盖未移动的结算分支
            if spec['skip'] and not moved_any and random.random() < 0.08:
                stats['skipped'] += 1
                break
            nxt = AI.next_step(room, pid, spec['ai'])
            if not nxt:
                stuck[pid] += 1
                stats['max_stuck'] = max(stats['max_stuck'], stuck[pid])
                break
            stuck[pid] = 0
            cost, color = cost_to(room, pid, nxt)
            tk_before = dict(p['tickets'])
            room.move(pid, nxt)
            tk_after = p['tickets']
            spent = tickets_total(tk_before) - tickets_total(tk_after)
            if spent != cost:
                raise Violation('%s 走 %s 扣票 %d≠票价 %d' % (pid, nxt, spent, cost))
            # 支付构成：优先真票、GO 只补缺口
            true_spent = tk_before[color] - tk_after[color]
            wild_spent = tk_before[room.M.WILD] - tk_after[room.M.WILD]
            if true_spent != min(cost, tk_before[color]) or wild_spent != cost - true_spent:
                raise Violation('%s 支付构成异常 真%d+GO%d(cost%d,有%d)'
                                % (pid, true_spent, wild_spent, cost, tk_before[color]))
            expect[pid] -= cost
            moved_any = True
            stats['moves'] += 1

        # ---- 结束回合 ----
        if room.state == 'playing' and room.phase == 'move':
            room.end_turn(pid)

        # ---- 每步不变量 ----
        for q in pids:
            check_player(room, q, {'score': prev_score[q]})
            prev_score[q] = room.players[q]['score']
            if tickets_total(room.players[q]['tickets']) != expect[q]:
                raise Violation('%s 手牌总数 %d≠应有 %d' %
                                (q, tickets_total(room.players[q]['tickets']), expect[q]))
        # 城市卡唯一性 / 桌面卡数量
        if len(room.shared_cards) != room.shared_n:
            raise Violation('桌面卡数量变化 %d' % len(room.shared_cards))

    else:
        return {'finished': False, 'winner': None, 'stats': stats, 'room': room}

    # ---- 终局校验 ----
    w = room.winner
    owners = collections.defaultdict(set)
    for q in pids:
        for c in room.players[q]['cards']:
            owners[c].add(q)
    res = {'finished': True, 'winner': w, 'stats': stats, 'room': room,
           'scores': {q: room.players[q]['score'] for q in pids},
           'dup_cards': {c: sorted(v) for c, v in owners.items() if len(v) > 1},
           'roll_spread': max(rolls.values()) - min(rolls.values())}
    if w is None:
        raise Violation('游戏结束但没有赢家')
    wp = room.players[w]
    by_score = '城市卡全部用完' in (room.log[-1] if room.log else '')
    stats['by_score'] = by_score
    if by_score:
        top = max(room.players[q]['score'] for q in pids)
        if wp['score'] != top:
            raise Violation('收官时赢家分数 %d 不是最高分 %d' % (wp['score'], top))
    else:
        if wp['score'] < room.win_score:
            raise Violation('赢家分数 %d < 目标 %d' % (wp['score'], room.win_score))
        if wp['position'] != wp['secret']:
            raise Violation('赢家未停在秘密目的地 %s≠%s' % (wp['position'], wp['secret']))
    # 分数构成：城市卡分 + 奖章分
    for q in pids:
        pp = room.players[q]
        card_sum = sum(room.M.CITY_POINTS.get(c, 0) for c in pp['cards'])
        medal_sum = room.medal_score * len(pp.get('medals', []))
        if card_sum + medal_sum != pp['score']:
            raise Violation('%s 分数 %d ≠ 卡%d+奖章%d' % (q, pp['score'], card_sum, medal_sum))
    return res      # 注：牌堆循环后同一城市可能再次被拿，属正常，故不再判重复


def main():
    specs = []
    for mod, name in ((W, '世界'), (S, '山河')):
        for n in (2, 4, 8):
            for steps in (1, 2, 3):
                specs.append({'mod': mod, 'name': name, 'n': n, 'steps': steps,
                              'win': 10, 'ai': 'normal', 'quiz_rate': 0.5,
                              'skip': True, 'auto': True})
        specs.append({'mod': mod, 'name': name, 'n': 3, 'steps': 1, 'win': 20,
                      'ai': 'easy', 'quiz_rate': 1.0, 'skip': False, 'auto': False})
        specs.append({'mod': mod, 'name': name, 'n': 10, 'steps': 1, 'win': 10,
                      'ai': 'normal', 'quiz_rate': 0.5, 'skip': True, 'auto': True})
        specs.append({'mod': mod, 'name': name, 'n': 4, 'steps': 2, 'win': 10,
                      'ai': 'normal', 'quiz_rate': 0.5, 'skip': True, 'auto': True,
                      'drop': True, 'thin_deck': True, 'tag后缀': '★牌堆耗尽+掉线'})
        specs.append({'mod': mod, 'name': name, 'n': 4, 'steps': 2, 'win': 10,
                      'ai': 'normal', 'quiz_rate': 0.5, 'skip': True, 'auto': True,
                      'drop': False, 'thin_deck': True, 'tag后缀': '★仅牌堆耗尽'})
        specs.append({'mod': mod, 'name': name, 'n': 4, 'steps': 2, 'win': 10,
                      'ai': 'normal', 'quiz_rate': 0.5, 'skip': True, 'auto': True,
                      'drop': True, 'thin_deck': False, 'tag后缀': '★仅掉线'})
    bad = []
    by_cfg = collections.defaultdict(list)
    deck_left = collections.defaultdict(list)
    rescue = collections.defaultdict(list)
    for si, spec in enumerate(specs):
        for seed in range(6):
            tag = '%s n=%-2d steps=%d win=%d %s' % (spec['name'], spec['n'], spec['steps'],
                                                    spec['win'], spec.get('tag后缀', ''))
            try:
                r = simulate(spec, seed * 97 + si)
            except Violation as e:
                bad.append('%s seed=%d :: %s' % (tag, seed, e))
                continue
            except Exception as e:
                bad.append('%s seed=%d :: 崩溃 %r' % (tag, seed, e))
                continue
            if not r['finished']:
                rm = r['room']
                diag = []
                for q in pids_all(rm):
                    pp = rm.players[q]
                    d = rm.M.ALL_DIST.get(pp['position'], {}).get(pp['secret'], '?')
                    diag.append('%s 分%d 在%s 目的%s 距%s' %
                                (q, pp['score'], pp['position'], pp['secret'], d))
                bad.append('%s seed=%d :: 6000 步未结束 | 目标%d 桌面%s 牌堆%d | %s'
                           % (tag, seed, rm.win_score,
                              [c for c in rm.shared_cards if c], len(rm.deck),
                              ' ; '.join(diag)))
            else:
                by_cfg[tag].append(r['stats']['turns'])
                deck_left.setdefault(tag, []).append(len(r['room'].deck))
                if r['stats'].get('by_score'):
                    rescue.setdefault(tag, []).append(1)
    print('总对局', len(specs) * 6, '异常', len(bad))
    print('---- 各配置：结束所需【总回合数】（含所有玩家）----')
    for tag in sorted(by_cfg):
        v = sorted(by_cfg[tag])
        dl = sorted(deck_left.get(tag, []))
        print('  %-34s 最少 %3d 中位 %3d 最多 %3d | 牌堆最少剩 %2d | 卡用完收官 %d/%d 局'
              % (tag, v[0], v[len(v) // 2], v[-1], dl[0] if dl else -1,
                 len(rescue.get(tag, [])), len(v)))
    for b in bad[:25]:
        print('  ✗', b)
    if not bad:
        print('  ✓ 全部通过')


if __name__ == '__main__':
    main()
