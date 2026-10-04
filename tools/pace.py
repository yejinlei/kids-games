# -*- coding: utf-8 -*-
"""节奏调参：比较不同「每回合骰子数 / 步数」组合下一局的长度。

用法： python tools/pace.py
输出： 各组合的【总回合数中位数】与【人均回合数】，用来判断哪种设置玩起来不拖沓。

结论（世界之旅，12 局中位数，人均回合数）：
  1 步 2 骰 = 30 ；2 步 2 骰 = 55（更慢！票不够走满两步，且只有停留城市能拿卡）
  1 步 3 骰 = 28 ；2 步 3 骰 = 29 ；3 步 2 骰 = 77
-> 想提速应当「多给票（加骰子）」，而不是「多走步」。
"""
import os
import random
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import games.shijie.map_data as W
import games.shanhe.map_data as S
import core.journey as J
import core.ai as AI


def one(mod, n, steps, dice, win, seed):
    random.seed(seed)
    room = J.JourneyRoom(mod.build_map(), 'P', max_players=n, steps_per_turn=steps,
                         win_score=win, ai_level='normal', dice_count=dice)
    pids = ['p%d' % i for i in range(n)]
    for i, pid in enumerate(pids):
        room.join(pid, '玩家%d' % i, 's%d' % i)
    room.start(room.host_pid)
    turns = moves = 0
    for _ in range(8000):
        if room.state != 'playing':
            break
        pid = room.current_pid
        if room.phase == 'roll':
            room.roll(pid)
            turns += 1
        guard = 0
        while room.state == 'playing' and room.phase == 'move' and room.steps_left > 0:
            guard += 1
            if guard > 8:
                break
            nxt = AI.next_step(room, pid, 'normal')
            if not nxt:
                break
            room.move(pid, nxt)
            moves += 1
        if room.state == 'playing' and room.phase == 'move':
            room.end_turn(pid)
    return turns, moves


def main():
    for mod, name in ((W, '世界'), (S, '山河')):
        for n in (2, 4):
            for steps, dice in ((1, 2), (2, 2), (1, 3), (2, 3), (3, 2)):
                ts, mv = [], []
                for seed in range(12):
                    t, m = one(mod, n, steps, dice, 10, seed * 31 + n)
                    ts.append(t)
                    mv.append(m)
                ts.sort()
                mv.sort()
                print('%-4s %d人 %d步 %d骰 : 总回合中位 %3d（人均 %2d） | 总移动步数中位 %3d'
                      % (name, n, steps, dice, ts[len(ts) // 2],
                         ts[len(ts) // 2] // n, mv[len(mv) // 2]))


if __name__ == '__main__':
    main()
