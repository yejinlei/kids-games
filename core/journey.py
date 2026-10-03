# -*- coding: utf-8 -*-
"""通用"旅游棋"引擎：房间、回合、掷骰、移动、得分与胜负判定。

规则与《山河之旅》一致，地图由外部注入（core.gamemap.GameMap），
因此同一套引擎可同时支撑《山河之旅》《世界之旅》等不同棋盘。

线程安全：每个房间自带 RLock，调用方（网络层）在修改状态时需持有该锁。
"""
import random
import time
import threading


class GameError(Exception):
    pass


class JourneyRoom:
    def __init__(self, map_, room_id, password='', dice_count=None,
                 timeout=30, ai_level='normal', max_players=4,
                 win_score=None, steps_per_turn=1):
        self.M = map_          # 注入的地图（城市/线路/配色/陆地轮廓）
        self.room_id = room_id
        self.password = password
        # 可选规则（默认沿用正版：目标 10 分、每回合走 1 步）
        self.win_score = int(win_score) if win_score else map_.WIN_SCORE
        self.steps_per_turn = max(1, min(3, int(steps_per_turn or 1)))
        # 官方规则：每回合固定掷 2 颗骰子；允许通过参数覆盖，但至少 1 颗
        self.dice_count = dice_count if (isinstance(dice_count, int) and dice_count >= 1) else self.M.DICE_PER_TURN
        self.ai_level = ai_level   # easy | normal，机器人难度
        self.max_players = max(2, min(4, int(max_players)))  # 房间总人数上限（含机器人）
        self.timeout = max(10, min(120, int(timeout)))
        self.players = {}          # pid -> player dict
        self.order = []            # 加入顺序（用于回合轮转）
        self.sid_map = {}          # pid -> socketio sid
        self.host_pid = None
        self.creator_name = ''     # 房间创建者昵称（掉线也保留）
        self.state = 'lobby'       # lobby | playing | finished
        self.current_pid = None
        self.phase = 'wait'        # wait | roll | move
        self.turn_token = 0        # 每次回合切换递增，用于超时判断
        self.timer_deadline = None
        self.move_path = []        # 本回合移动经过的城市序列（结束时结算）
        self.shared_cards = []     # 中央翻开的 3 张城市卡
        self.deck = []             # 剩余城市卡牌堆
        self.last_roll = []
        self.winner = None
        self.finish_order = []     # 依次达成目标的玩家 pid（游戏结束条件用）
        self.log = []
        self.lock = threading.RLock()
        self.TICKET_KEYS = list(self.M.COLORS.keys()) + [self.M.WILD]
        self._build_adj()

    @property
    def steps_left(self):
        """本回合还能走几步（走满后引擎会自动结束回合）。"""
        return max(0, self.steps_per_turn - max(0, len(self.move_path) - 1))

    # ---------- 地图辅助 ----------
    def _build_adj(self):
        self.adj = {c: [] for c in self.M.CITY_GEO}
        for i, (a, b, color, cost) in enumerate(self.M.ROUTES):
            self.adj[a].append({'to': b, 'color': color, 'cost': cost, 'id': i})
            self.adj[b].append({'to': a, 'color': color, 'cost': cost, 'id': i})

    def neighbors(self, city):
        return self.adj.get(city, [])

    # ---------- 玩家管理 ----------
    def available_colors(self):
        used = {p['color'] for p in self.players.values() if p['color']}
        return [c for c in self.M.COLORS if c not in used]

    def join(self, pid, name, sid, bot=False):
        if pid in self.players:
            # 重连：更新 sid 与在线状态
            self.players[pid]['sid'] = sid
            self.players[pid]['connected'] = True
            self.sid_map[pid] = sid
            self.log.append(f'{name} 重新连接')
            return
        color = self.available_colors()[0] if self.available_colors() else None
        self.players[pid] = {
            'pid': pid, 'name': name, 'color': color, 'sid': sid,
            'position': '北京', 'score': 0, 'goal_done': False,
            'secret': None, 'tickets': {k: 0 for k in self.TICKET_KEYS},
            'cards': [],               # 已收走的城市卡（公开信息）
            'connected': True, 'bot': bool(bot),
        }
        self.sid_map[pid] = sid
        self.order.append(pid)
        if self.host_pid is None and not bot:
            self.host_pid = pid
            self.creator_name = name
        self.log.append(f'{name} 加入了房间' + ('（机器人）' if bot else ''))

    def is_bot(self, pid):
        return bool(pid and self.players.get(pid, {}).get('bot'))

    def mark_disconnected(self, pid):
        p = self.players.get(pid)
        if not p:
            return
        p['connected'] = False
        if pid == self.host_pid:
            # 房主掉线，转移给下一位在线玩家
            for nxt in self.order:
                if nxt != pid and self.players[nxt]['connected']:
                    self.host_pid = nxt
                    break
        self.log.append(f'{p["name"]} 断开了连接')

    def connected_pids(self):
        return [pid for pid in self.order if self.players[pid]['connected']]

    # ---------- 游戏开始 ----------
    def start(self, pid):
        if pid != self.host_pid:
            raise GameError('只有房主可以开始游戏')
        if len(self.connected_pids()) < 2:
            raise GameError('至少需要 2 名在线玩家')
        if self.state != 'lobby':
            raise GameError('游戏已经开始')
        deck = [c for c in self.M.CITY_GEO if c != '北京']
        random.shuffle(deck)
        self.deck = deck
        for p in self.players.values():
            p['position'] = '北京'
            p['score'] = 0
            p['goal_done'] = False
            p['tickets'] = {k: 0 for k in self.TICKET_KEYS}
            p['cards'] = []
            p['secret'] = self.deck.pop()
        self.shared_cards = [self.deck.pop() for _ in range(min(3, len(self.deck)))]
        self.state = 'playing'
        self.current_pid = self.connected_pids()[0]
        self.begin_turn()
        self.log.append('游戏开始！祝大家旅途愉快～')

    def begin_turn(self):
        self.turn_token += 1
        conn = self.connected_pids()
        if self.current_pid not in conn:
            self.current_pid = conn[0] if conn else None
        self.phase = 'roll'
        self.last_roll = []
        self.move_path = []
        self.timer_deadline = time.time() + self.timeout

    # ---------- 掷骰子 ----------
    def _face_label(self, f):
        if f == self.M.WILD:
            return 'GO(万能)'
        return self.M.COLORS[f]['name']

    def roll(self, pid):
        if self.state != 'playing':
            raise GameError('游戏未开始')
        if pid != self.current_pid:
            raise GameError('还没轮到你')
        if self.phase != 'roll':
            raise GameError('本回合已经掷过骰子')
        results = [random.choice(self.M.DICE_FACES) for _ in range(self.dice_count)]
        self.last_roll = results
        p = self.players[pid]
        for f in results:
            p['tickets'][self.M.WILD if f == self.M.WILD else f] += 1
        self.phase = 'move'
        self.log.append(f'{p["name"]} 掷出：' + '、'.join(self._face_label(f) for f in results))

    # ---------- 移动 ----------
    def can_traverse(self, pid, neighbor):
        p = self.players[pid]
        for r in self.neighbors(p['position']):
            if r['to'] == neighbor:
                have = p['tickets'][r['color']] + p['tickets'][self.M.WILD]
                return have >= r['cost']
        return False

    def reachable_moves(self, pid):
        """本回合可以走的【下一步】：当前城市的相邻城市中，车票足够的那些。"""
        p = self.players[pid]
        out = []
        for r in self.neighbors(p['position']):
            if p['tickets'][r['color']] + p['tickets'][self.M.WILD] >= r['cost']:
                out.append({'to': r['to'], 'color': r['color'], 'cost': r['cost']})
        return out

    def move(self, pid, neighbor, color=None):
        """走【一步】：沿一条相邻线路移动，消耗该颜色车票（GO 可顶替）。
        官方规则：每回合只能沿一条线路移动一次，移动后立即结束本回合。
        """
        if self.state != 'playing':
            raise GameError('游戏未开始')
        if pid != self.current_pid:
            raise GameError('还没轮到你')
        if self.phase != 'move':
            raise GameError('请先掷骰子')
        p = self.players[pid]
        routes = [r for r in self.neighbors(p['position']) if r['to'] == neighbor]
        if color:
            same = [r for r in routes if r['color'] == color]
            routes = same or routes
        if not routes:
            raise GameError('无法从当前城市到达该城市')
        # 优先用真票、少消耗万能票
        routes.sort(key=lambda r: (r['cost'], max(0, r['cost'] - p['tickets'][r['color']])))
        route = routes[0]
        if p['tickets'][route['color']] + p['tickets'][self.M.WILD] < route['cost']:
            raise GameError('该路线车票不足')
        use_color = min(route['cost'], p['tickets'][route['color']])
        use_wild = route['cost'] - use_color
        p['tickets'][route['color']] -= use_color
        p['tickets'][self.M.WILD] -= use_wild
        if not self.move_path:
            self.move_path = [p['position']]
        p['position'] = neighbor
        self.move_path.append(neighbor)
        self.log.append(
            f'{p["name"]} 沿{self.M.COLORS[route["color"]]["name"]}色线路消耗 {route["cost"]} 张票，前往 {neighbor}')
        # 走满本回合允许的步数后，自动结算并结束回合
        # （正版为每回合 1 步；世界之旅等开放棋盘可选更多步数）
        if len(self.move_path) - 1 >= self.steps_per_turn:
            self._advance()

    def _claims(self, p, path=None):
        city = p['position']
        path = path or [city]
        # 官方规则：只有移动后停留的城市与桌面城市卡相同，才获得该城市卡上的分数
        if city in self.shared_cards:
            idx = self.shared_cards.index(city)
            pts = self.M.CITY_POINTS[city]
            p['score'] += pts
            p.setdefault('cards', []).append(city)
            self.log.append(f'{p["name"]} 获得城市卡 {city}，+{pts} 分！')
            # 步骤四：翻开一张新的城市卡，补齐桌面 3 张
            self.shared_cards[idx] = self.deck.pop() if self.deck else None
        # 秘密目的地：只有【当前停留】在秘密目的地才算到达（途经不算）
        arrived = (city == p['secret'])
        if arrived and p['score'] >= self.win_score:
            p['goal_done'] = True
            self.log.append(f'{p["name"]} 抵达秘密目的地 {city}！')
        # 获胜条件（官方）：分数 ≥ self.win_score 且 当前停留在秘密目的地，立即获胜并结束游戏
        if self.state == 'playing' and p['score'] >= self.win_score and arrived:
            self.state = 'finished'
            self.winner = p['pid']
            self.phase = 'done'
            self.timer_deadline = None
            self.log.append(f'🎉 游戏结束！{p["name"]} 抵达秘密目的地，达成目标，获胜！')
            return

    # ---------- 回合结束 ----------
    def end_turn(self, pid):
        if self.state != 'playing':
            raise GameError('游戏未开始')
        if pid != self.current_pid:
            raise GameError('还没轮到你')
        self._advance()

    def _advance(self):
        self._finalize_move()
        if self.state != 'playing':
            return
        conn = self.connected_pids()
        if not conn:
            return
        # 已达成目标（停在秘密目的地且分数达标）的玩家轮空，不再参与回合轮转
        done = {pid for pid, q in self.players.items()
                if q['position'] == q['secret'] and q['score'] >= self.win_score}
        pool = [pid for pid in conn if pid not in done] or conn
        idx = pool.index(self.current_pid) if self.current_pid in pool else -1
        self.current_pid = pool[(idx + 1) % len(pool)]
        self.begin_turn()

    def _finalize_move(self):
        """本回合移动结束（点结束回合 / 移动一次后自动 / 超时）时结算：
        只有【最后停留的城市】能拿城市卡，也只有它用于判定秘密目的地（途经不算）。
        """
        pid, path = self.current_pid, self.move_path
        self.move_path = []
        p = self.players.get(pid) if pid else None
        if not p or len(path) < 2:
            return
        self._claims(p, path)

    def auto_play(self, token):
        """超时自动行动：
        - 还没掷骰：自动掷；
        - 已掷但未移动：自动走一步（引擎内部会结算并结束回合）；
        - 实在无路可走：跳过本回合（结束回合）。
        """
        if token != self.turn_token or self.state != 'playing':
            return
        pid = self.current_pid
        p = self.players.get(pid)
        if not p:
            return
        if self.phase == 'roll':
            try:
                self.roll(pid)
            except GameError:
                pass
        if self.state == 'playing' and self.phase == 'move':
            moves = self.reachable_moves(pid)
            if moves:
                try:
                    self.move(pid, moves[0]['to'])
                    self.log.append(f'⏰ {p["name"]} 超时，自动前进了一步')
                except GameError:
                    pass
        # 若回合仍未结束（没走成），则正常结束本回合
        if self.state == 'playing' and self.phase != 'roll':
            self._advance()

    # ---------- 序列化 ----------
    def serialize(self, viewer_pid=None):
        players = []
        for pid in self.order:
            p = self.players.get(pid)
            if not p:
                continue
            d = {
                'pid': pid, 'name': p['name'], 'color': p['color'],
                'position': p['position'], 'score': p['score'],
                'goal_done': p['goal_done'], 'connected': p['connected'],
                'tickets': dict(p['tickets']), 'bot': p['bot'],
                'cards': list(p.get('cards', [])),
                'is_current': pid == self.current_pid,
            }
            if viewer_pid == pid:
                d['secret'] = p['secret']
            players.append(d)
        moves = []
        if viewer_pid == self.current_pid and self.state == 'playing' and self.phase == 'move':
            moves = self.reachable_moves(viewer_pid)
        shared = [{'city': c, 'points': self.M.CITY_POINTS.get(c, 0) if c else 0} for c in self.shared_cards]
        rem = 0
        if self.timer_deadline and self.state == 'playing':
            rem = max(0, int(self.timer_deadline - time.time()))
        return {
            'room_id': self.room_id,
            'state': self.state,
            'host_pid': self.host_pid,
            'dice_count': self.dice_count,
            'timeout': self.timeout,
            'win_score': self.win_score,
            'steps_per_turn': self.steps_per_turn,
            'steps_left': self.steps_left,
            'players': players,
            'shared_cards': shared,
            'current_pid': self.current_pid,
            'phase': self.phase,
            'moves': moves,
            'moved': len(self.move_path) > 1,
            'move_path': list(self.move_path),
            'last_roll': self.last_roll,
            'winner': self.winner,
            'timer_remaining': rem,
            'log': self.log[-40:],
        }
