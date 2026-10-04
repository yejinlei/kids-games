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


# 出发城市：房主不指定具体城市时，开局随机揭晓
RANDOM_START = 'random'


CHAT_MAX_LEN = 200      # 单条聊天字数上限
CHAT_KEEP = 100         # 每个房间保留的聊天条数

# 地图本身只有 5 种车票色（最多区分 5 名玩家）。房间支持到 10 人，因此额外补一组
# 只用于「玩家身份」的颜色——不参与车票/线路，纯用于棋子和记分区分。
EXTRA_PLAYER_COLORS = {
    'orange': {'hex': '#e67e22', 'name': '橙'},
    'teal':   {'hex': '#16a085', 'name': '青'},
    'pink':   {'hex': '#e84393', 'name': '粉'},
    'brown':  {'hex': '#8d6e63', 'name': '棕'},
    'slate':  {'hex': '#546e7a', 'name': '灰蓝'},
}
MAX_PLAYERS = 10        # 房间总人数上限（含机器人）


class JourneyRoom:
    def __init__(self, map_, room_id, password='', dice_count=None,
                 timeout=30, ai_level='normal', max_players=4,
                 win_score=None, steps_per_turn=1, start_city=None):
        self.M = map_          # 注入的地图（城市/线路/配色/陆地轮廓）
        self.room_id = room_id
        self.password = password
        # 可选规则（默认沿用正版：目标 10 分、每回合走 1 步）
        self.win_score = int(win_score) if win_score else map_.WIN_SCORE
        self.steps_per_turn = max(1, min(3, int(steps_per_turn or 1)))
        # 官方规则：每回合固定掷 2 颗骰子；允许通过参数覆盖，但至少 1 颗
        self.dice_count = dice_count if (isinstance(dice_count, int) and dice_count >= 1) else self.M.DICE_PER_TURN
        self.ai_level = ai_level   # easy | normal，机器人难度
        self.max_players = max(2, min(MAX_PLAYERS, int(max_players)))  # 房间总人数上限（含机器人）
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
        self.host_history = set()  # 掉线时让出房主的原房主（重连后归还）
        self.log = []
        self.messages = []         # 房间聊天记录（所有人可见，保留最近 CHAT_KEEP 条）
        self.lock = threading.RLock()
        self.TICKET_KEYS = list(self.M.COLORS.keys()) + [self.M.WILD]
        # 学习玩法开关（由地图决定，例如《世界之旅》开启文化卡/护照/奖章）
        self.features = set(self.M.FEATURES)
        self.medal_cities = self.M.MEDAL_CITIES
        self.medal_score = self.M.MEDAL_SCORE
        self.shared_n = self.M.SHARED_N
        # 出发城市：房主可指定（默认沿用地图的 START_CITY），或用 RANDOM_START 开局随机
        self.start_city = self._norm_start(start_city)
        self._build_adj()

    # ---------- 出发城市 ----------
    def _norm_start(self, city):
        """规范化出发城市：非法值回退到地图默认；RANDOM_START 表示开局随机揭晓。"""
        if city == RANDOM_START:
            return RANDOM_START
        if city and city in self.M.CITY_GEO:
            return city
        return self.M.START_CITY

    def _random_start(self):
        """随机挑出发城市：优先航线较多（>=3 条）的城市，免得开局就困在死角。"""
        rich = [c for c in self.M.CITY_GEO if len(self.adj.get(c, [])) >= 3]
        pool = rich or [c for c in self.M.CITY_GEO if len(self.adj.get(c, [])) >= 2]
        return random.choice(pool or list(self.M.CITY_GEO))

    def start_display(self):
        """当前展示的出发城市（随机且尚未开局时，用地图默认值占位）。"""
        if self.start_city == RANDOM_START and self.state == 'lobby':
            return self.M.START_CITY
        return self.start_city

    def set_start_city(self, pid, city):
        """房主在等待阶段改出发城市（开局后不可改）。"""
        if pid != self.host_pid:
            raise GameError('只有房主可以设置出发城市')
        if self.state != 'lobby':
            raise GameError('游戏已开始，无法修改出发城市')
        self.start_city = self._norm_start(city)
        pos = self.start_display()
        for p in self.players.values():
            p['position'] = pos
        self.log.append('🛫 出发城市设为 ' +
                        ('🎲 随机（开局揭晓）' if self.start_city == RANDOM_START else self.start_city))

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
        """可用玩家色：先用地图的车票色，用完再补 EXTRA_PLAYER_COLORS，保证 10 人不撞色。"""
        used = {p['color'] for p in self.players.values() if p['color']}
        return ([c for c in self.M.COLORS if c not in used] +
                [c for c in EXTRA_PLAYER_COLORS if c not in used])

    def join(self, pid, name, sid, bot=False):
        if pid in self.players:
            # 重连：更新 sid 与在线状态
            self.players[pid]['sid'] = sid
            self.players[pid]['connected'] = True
            self.sid_map[pid] = sid
            if pid in self.host_history and self.is_bot(self.host_pid):
                # 原房主重连且房主 currently 在机器人手里：把房主还给 TA，
                # 否则机器人当房主永远开不了下一局（"再来一局"会卡死）
                self.host_history.discard(pid)
                self.host_pid = pid
                self.log.append(f'{name} 回归，重新担任房主')
            self.log.append(f'{name} 重新连接')
            return
        color = self.available_colors()[0] if self.available_colors() else None
        self.players[pid] = {
            'pid': pid, 'name': name, 'color': color, 'sid': sid,
            'position': self.start_display(), 'score': 0, 'goal_done': False,
            'secret': None, 'tickets': {k: 0 for k in self.TICKET_KEYS},
            'cards': [],               # 已收走的城市卡（公开信息）
            'connected': True, 'bot': bool(bot),
            # 学习玩法：护照印章 / 大洲奖章 / 待答知识题 / 答题统计
            'passport': {},            # 城市 -> {country, flag, zone, year}
            'medals': [],              # 已获得奖章的大洲
            'quiz': None,              # 待回答的知识题 {city, q, o, cat}
            'quiz_ok': 0, 'quiz_no': 0,
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
            # 房主掉线：优先转给在线真人（机器人不会开局，交给它们会卡死房间）；
            # 并记下原房主，等 TA 重连时把房主还回去
            self.host_history.add(pid)
            humans = [n for n in self.order
                      if n != pid and self.players[n]['connected'] and not self.players[n]['bot']]
            anyone = [n for n in self.order
                      if n != pid and self.players[n]['connected']]
            for nxt in (humans or anyone):
                self.host_pid = nxt
                break
        self.log.append(f'{p["name"]} 断开了连接')

    def connected_pids(self):
        return [pid for pid in self.order if self.players[pid]['connected']]

    # ---------- 房间聊天 ----------
    def add_chat(self, pid, text):
        """发一条房间聊天（房间内所有人可见）。返回是否真的发出去了。"""
        p = self.players.get(pid)
        if not p:
            return False
        t = ' '.join((text or '').split())       # 去掉首尾空白与多余换行
        if not t:
            return False
        self.messages.append({
            'pid': pid, 'name': p['name'], 'color': p['color'],
            'bot': bool(p['bot']), 'text': t[:CHAT_MAX_LEN], 'ts': int(time.time()),
        })
        if len(self.messages) > CHAT_KEEP:
            del self.messages[:len(self.messages) - CHAT_KEEP]
        return True

    # ---------- 游戏开始 ----------
    def start(self, pid):
        if pid != self.host_pid:
            raise GameError('只有房主可以开始游戏')
        if len(self.connected_pids()) < 2:
            raise GameError('至少需要 2 名在线玩家')
        if self.state not in ('lobby', 'finished'):
            raise GameError('游戏已经开始')
        if self.state == 'finished':
            self.winner = None
            self.finish_order = []
            self.log.append('🔁 再来一局！上届成绩已清零')
        if self.start_city == RANDOM_START:
            self.start_city = self._random_start()
            self.log.append(f'🎲 本届出发城市揭晓：{self.start_city}')
        deck = [c for c in self.M.CITY_GEO if c != self.start_city]
        random.shuffle(deck)
        self.deck = deck
        for p in self.players.values():
            p['position'] = self.start_city
            p['score'] = 0
            p['goal_done'] = False
            p['tickets'] = {k: 0 for k in self.TICKET_KEYS}
            p['cards'] = []
            p['passport'] = {}
            p['medals'] = []
            p['quiz'] = None
            p['quiz_ok'] = 0
            p['quiz_no'] = 0
            p['secret'] = self.deck.pop()
        self.shared_cards = [self.deck.pop() for _ in range(min(self.shared_n, len(self.deck)))]
        self.state = 'playing'
        self.current_pid = self.connected_pids()[0]
        self.begin_turn()
        self.log.append('游戏开始！祝大家旅途愉快～')

    def begin_turn(self):
        self.turn_token += 1
        conn = self.connected_pids()
        if self.current_pid not in conn:
            self.current_pid = conn[0] if conn else None
        # 上一站遗留未答的知识题作废，避免题目堆积
        p = self.players.get(self.current_pid)
        if p and p.get('quiz'):
            p['quiz'] = None
            self.log.append(f'⏳ {p["name"]} 上一站的题目没有作答，已跳过')
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

    def _draw_card(self):
        """翻开下一张城市卡；牌堆用尽就不再补（该位置空着），随后由 _finish_by_score 收官。"""
        return self.deck.pop() if self.deck else None

    def _no_more_points(self):
        """牌堆和桌面都没有城市卡了：谁也不可能再得分。"""
        return not self.deck and not [c for c in self.shared_cards if c]

    def _finish_by_score(self):
        """城市卡全部用完：没人能再凑分，按分数收官（同为最高分时，踩中秘密目的地者优先）。"""
        best = None
        for pid in self.order:
            p = self.players.get(pid)
            if not p:
                continue
            key = (p['score'], 1 if p['position'] == p['secret'] else 0)
            if best is None or key > best[0]:
                best = (key, pid)
        if not best:
            return
        self.state = 'finished'
        self.winner = best[1]
        self.phase = 'done'
        self.timer_deadline = None
        w = self.players[best[1]]
        self.log.append('🎴 城市卡全部用完了，旅行结束！' + w['name'] +
                        ' 以最高分 ' + str(w['score']) + ' 分获胜！')

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
            # 步骤四：翻开一张新的城市卡，补齐桌面上的空位
            self.shared_cards[idx] = self._draw_card()
        # 学习玩法：盖护照章、广播地理/历史见闻、发一道知识题
        self._culture_arrival(p, city)
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

    # ---------- 学习玩法：护照 / 见闻 / 知识问答 / 大洲奖章 ----------
    def _culture_arrival(self, p, city):
        """抵达一座城市：盖护照章、广播地理与历史见闻、出一道知识题、检查大洲奖章。"""
        if 'culture' not in self.features:
            return
        info = self.M.CITY_INFO.get(city)
        if not info:
            return
        stamp = p.setdefault('passport', {})
        if city in stamp:               # 已经盖过章的城市不再重复出题
            return
        stamp[city] = {'country': info.get('country', ''), 'flag': info.get('flag', ''),
                       'zone': info.get('zone', ''), 'year': info.get('year')}
        self.log.append(f'🛂 {p["name"]} 的护照盖上了 {info.get("flag", "")}'
                        f'{info.get("country", "")} · {city} 的印章')
        # 见闻广播给所有人：别的孩子也能看到，边看边学
        if info.get('geo'):
            self.log.append(f'📍 地理 · {city}：{info["geo"]}')
        if info.get('hist'):
            y = info.get('year')
            if isinstance(y, int):
                when = '公元前 %d 年' % abs(y) if y < 0 else '%d 年' % y
            else:
                when = ''
            self.log.append(f'🏛 历史 · {city}（{when}）：{info["hist"]}')
        if info.get('q') and info.get('o'):
            p['quiz'] = {'city': city, 'q': info['q'], 'o': list(info['o']),
                         'cat': info.get('cat', '')}
        self._check_medal(p, info.get('zone', ''))

    def _check_medal(self, p, zone):
        """集齐同一大洲若干座城市，获得该大洲奖章（加分，一次性）。"""
        if 'medal' not in self.features or not zone:
            return
        got = sum(1 for v in p.get('passport', {}).values() if v.get('zone') == zone)
        if got >= self.medal_cities and zone not in p.setdefault('medals', []):
            p['medals'].append(zone)
            p['score'] += self.medal_score
            self.log.append(f'🏅 {p["name"]} 走遍 {zone} {got} 座城市，'
                            f'获得【{zone}奖章】+{self.medal_score} 分！')

    def answer_quiz(self, pid, idx):
        """回答知识题：答对得 1 张 GO 万能票；答错也给正确答案和解释，**不惩罚**。

        返回本次答题详情（供前端展示解析）。
        """
        p = self.players.get(pid)
        if not p or not p.get('quiz'):
            raise GameError('现在没有要回答的问题')
        q = p['quiz']
        info = self.M.CITY_INFO.get(q['city'], {})
        opts = info.get('o') or q.get('o') or []
        right = info.get('a', 0)
        ok = (idx == right)
        p['quiz'] = None
        if ok:
            p['quiz_ok'] = p.get('quiz_ok', 0) + 1
            p['tickets'][self.M.WILD] = p['tickets'].get(self.M.WILD, 0) + 1
            self.log.append(f'✅ {p["name"]} 答对了（{q.get("cat", "")}）：'
                            f'{q["q"]} → 获得 1 张 GO 万能票')
        else:
            p['quiz_no'] = p.get('quiz_no', 0) + 1
            good = opts[right] if 0 <= right < len(opts) else '？'
            self.log.append(f'❌ {p["name"]} 答错了，正确答案是「{good}」。{info.get("why", "")}')
        return {'ok': ok, 'city': q['city'], 'cat': q.get('cat', ''),
                'right': right, 'right_text': opts[right] if 0 <= right < len(opts) else '',
                'why': info.get('why', ''), 'geo': info.get('geo', ''),
                'hist': info.get('hist', ''), 'flag': info.get('flag', ''),
                'country': info.get('country', '')}

    def auto_answer_quiz(self, pid):
        """机器人自动答题：按难度以一定概率答对，返回答题详情或 None。"""
        p = self.players.get(pid)
        if not p or not p.get('quiz'):
            return None
        info = self.M.CITY_INFO.get(p['quiz']['city'], {})
        n = len(info.get('o') or p['quiz']['o'] or [])
        right = info.get('a', 0)
        hit = 0.5 if self.ai_level == 'easy' else 0.75
        if n and random.random() < hit:
            idx = right
        else:
            idx = random.randrange(max(1, n))
            if n > 1 and idx == right:
                idx = (right + 1) % n      # 故意答错
        return self.answer_quiz(pid, idx)

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
        # 城市卡彻底用完（牌堆空 + 桌面空）：再无人能得分，按分数收官，避免整局僵死
        if self._no_more_points():
            self._finish_by_score()
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
                # 学习玩法（公开信息，方便孩子互相看护照）
                'passport': dict(p.get('passport', {})),
                'medals': list(p.get('medals', [])),
                'quiz_ok': p.get('quiz_ok', 0), 'quiz_no': p.get('quiz_no', 0),
            }
            if viewer_pid == pid:
                d['secret'] = p['secret']
                if p.get('quiz'):
                    d['quiz'] = p['quiz']
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
            'max_players': self.max_players,
            'dice_count': self.dice_count,
            'timeout': self.timeout,
            'win_score': self.win_score,
            'steps_per_turn': self.steps_per_turn,
            'start_city': self.start_city,
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
            'log': self.log[-60:],
            'messages': list(self.messages),
            # 学习玩法配置
            'features': sorted(self.features),
            'medal_cities': self.medal_cities,
            'medal_score': self.medal_score,
        }
