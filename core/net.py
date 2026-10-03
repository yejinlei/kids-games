# -*- coding: utf-8 -*-
"""通用网络层：把一款棋注册到 Flask + Flask-SocketIO。

每款棋拥有：
- 独立的房间字典 rooms（房间号只在本棋内唯一，不会跨棋冲突）
- 独立的 Socket.IO 命名空间 /<game_id>
- 独立的 HTTP 接口 /g/<game_id>/api/map 与 /api/rooms/<game_id>

新增一款棋 = 写一份 map_data.py + 注册 GameSpec，无需改动网络与引擎。
"""
import random
import re
import threading
import time
import uuid

from flask import jsonify, request
from flask_socketio import emit

from core.journey import JourneyRoom, GameError
import core.ai as ai


def register_game(app, socketio, spec):
    """把一款棋的全部 HTTP / Socket 接口挂到 app 上。"""
    gid = spec.id
    ns = '/' + gid
    rooms = {}
    rooms_lock = threading.RLock()

    BOT_NAMES = ['🤖小旅', '🤖山河', '🤖骆驼', '🤖指南针']

    # -------------------- HTTP --------------------
    # endpoint 必须按游戏区分，否则注册第二款棋时 Flask 会因重名报错
    @app.route('/g/%s/api/map' % gid, endpoint='%s_api_map' % gid)
    def api_map():
        return jsonify(spec.map.to_json())

    @app.route('/api/rooms/%s' % gid, endpoint='%s_api_rooms' % gid)
    def api_rooms():
        """房间列表：等待中且未满的可加入；进行中/已满/已结束的也返回（灰显展示），
        让玩家永远能找到自己创建的房间，而不是疑惑「房间去哪了」。"""
        out = []
        with rooms_lock:
            for rid, room in rooms.items():
                with room.lock:
                    full = len(room.players) >= room.max_players
                    out.append({
                        'room_id': rid,
                        'game': gid,
                        'creator': room.creator_name or '房主',
                        'has_password': bool(room.password),
                        'humans': sum(1 for p in room.players.values() if not p['bot']),
                        'bots': sum(1 for p in room.players.values() if p['bot']),
                        'current': len(room.players),
                        'max_players': room.max_players,
                        'dice_count': room.dice_count,
                        'timeout': room.timeout,
                        'start_city': room.start_city,
                        'state': room.state,
                        'joinable': room.state == 'lobby' and not full,
                    })
        out.sort(key=lambda r: (not r['joinable'], r['room_id']))
        return jsonify({'rooms': out})

    # -------------------- 工具 --------------------
    def gen_room_id():
        while True:
            rid = ''.join(random.choices('0123456789', k=4))
            if rid not in rooms:
                return rid

    def get_room(data):
        return rooms.get((data or {}).get('room_id'))

    def broadcast(room):
        with room.lock:
            for pid, p in list(room.players.items()):
                sid = room.sid_map.get(pid)
                if not sid or not p['connected']:
                    continue
                socketio.emit('state', room.serialize(pid), to=sid, namespace=ns)

    def bot_display_name(room):
        used = {p['name'] for p in room.players.values()}
        for n in BOT_NAMES:
            if n not in used:
                return n
        return '🤖旅伴' + str(len(room.players) + 1)

    def maybe_bot_turn(room):
        """若当前回合是机器人，启动它的自动行动（后台任务）。"""
        if room.state != 'playing':
            return
        pid = room.current_pid
        if pid and room.is_bot(pid):
            socketio.start_background_task(bot_play, room, pid)

    def bot_play(room, pid):
        """机器人一个完整回合：掷骰 -> 走一步 -> 回合自动结束。"""
        time.sleep(0.8)
        with room.lock:
            if room.state != 'playing' or room.current_pid != pid or room.phase != 'roll':
                return
            try:
                room.roll(pid)
            except GameError:
                return
            broadcast(room)
        time.sleep(0.7)

        for _ in range(15):     # 官方每回合只能走 1 步，走完回合已自动结束
            moved = False
            with room.lock:
                # 注意：这里必须 break 而非 return，否则会跳过循环后的
                # schedule_timer / maybe_bot_turn，导致下一回合无人驱动而卡死
                if room.state != 'playing' or room.current_pid != pid or room.phase != 'move':
                    break
                city = ai.next_step(room, pid, room.ai_level)
                if not city:
                    break
                try:
                    room.move(pid, city)
                    moved = True
                except GameError:
                    break
                broadcast(room)
            # 未走满步数时回合仍在进行，循环继续走下一步；
            # 走满后引擎会自动结束回合，下一轮由上面的守卫 break 掉
            time.sleep(0.6)

        # 机器人的知识问答：自动作答（答案与讲解会写进日志，孩子也能顺带学到）
        with room.lock:
            try:
                room.auto_answer_quiz(pid)
            except GameError:
                pass
            broadcast(room)
        time.sleep(0.5)

        with room.lock:
            # 若一步都没走成（仍在该机器人回合），则结束回合（结算/轮空）
            if room.state == 'playing' and room.current_pid == pid and room.phase == 'move':
                try:
                    room.end_turn(pid)
                except GameError:
                    pass
                broadcast(room)
        # 移动已自动结束本回合：为下一回合排程超时定时器，并触发下一位机器人
        if room.state == 'playing':
            schedule_timer(room)
            maybe_bot_turn(room)

    def schedule_timer(room):
        """为当前回合安排超时定时器（基于 turn_token 防止误触发）。"""
        token = room.turn_token

        def run():
            time.sleep(room.timeout)
            with rooms_lock:
                r = rooms.get(room.room_id)
            if not r:
                return
            with r.lock:
                if r.turn_token == token and r.state == 'playing':
                    r.auto_play(token)
                    broadcast(r)
                    if r.state == 'playing':
                        schedule_timer(r)
                        maybe_bot_turn(r)

        socketio.start_background_task(run)

    def require_player(data):
        """返回 (room, pid, player) 或失败时返回 None。"""
        room = get_room(data)
        if not room:
            emit('error', {'msg': '房间不存在'})
            return None
        pid = (data or {}).get('player_id')
        p = room.players.get(pid)
        if not p:
            emit('error', {'msg': '玩家不存在，请重新加入'})
            return None
        return room, pid, p

    # -------------------- Socket 事件 --------------------
    @socketio.on('create_room', namespace=ns)
    def create_room(data):
        data = data or {}
        name = (data.get('name') or '玩家').strip()[:12] or '玩家'
        password = (data.get('password') or '').strip()
        dice_count = max(1, int(data.get('dice_count', 2) or 2))
        timeout = int(data.get('timeout', 30) or 30)
        # 可选规则：目标分与每回合步数（不传则沿用该棋盘的默认值）
        try:
            win_score = int(data.get('win_score', 0) or 0) or None
            if win_score:
                win_score = max(5, min(30, win_score))
        except (TypeError, ValueError):
            win_score = None
        try:
            steps_per_turn = max(1, min(3, int(data.get('steps_per_turn', 1) or 1)))
        except (TypeError, ValueError):
            steps_per_turn = 1
        ai_level = (data.get('ai_level') or 'normal').strip()
        if ai_level not in ('easy', 'normal'):
            ai_level = 'normal'
        try:
            max_players = max(2, min(4, int(data.get('max_players', 4) or 4)))
        except (TypeError, ValueError):
            max_players = 4
        try:
            bot_count = max(0, min(3, int(data.get('bot_count', 0) or 0)))
        except (TypeError, ValueError):
            bot_count = 0
        # 出发城市：房主指定，或 'random'（开局揭晓）；不传则沿用棋盘默认
        start_city = (data.get('start_city') or '').strip()
        desired = (data.get('room_id') or '').strip()
        if desired:
            if not re.fullmatch(r'\d{4}', desired):
                emit('error', {'msg': '房间号须为 4 位数字'})
                return
            if desired in rooms:
                emit('error', {'msg': '房间号已被占用，换一个试试'})
                return
            rid = desired
        else:
            rid = gen_room_id()
        with rooms_lock:
            room = JourneyRoom(spec.map, rid, password, dice_count, timeout,
                               ai_level=ai_level, max_players=max_players,
                               win_score=win_score, steps_per_turn=steps_per_turn,
                               start_city=start_city)
            rooms[rid] = room
        pid = str(uuid.uuid4())
        with room.lock:
            room.join(pid, name, request.sid)
            # 创建时按设定加入机器人（受总人数上限约束）
            can_add = min(bot_count, max_players - 1, 3)
            for _ in range(can_add):
                bot_pid = 'bot:' + uuid.uuid4().hex[:8]
                room.join(bot_pid, bot_display_name(room), None, bot=True)
        emit('created', {'room_id': rid, 'player_id': pid})
        broadcast(room)

    @socketio.on('join_room', namespace=ns)
    def join_room(data):
        data = data or {}
        rid = (data.get('room_id') or '').strip()
        name = (data.get('name') or '玩家').strip()[:12] or '玩家'
        password = (data.get('password') or '').strip()
        room = rooms.get(rid)
        if not room:
            emit('error', {'msg': '房间不存在'})
            return
        if room.password and room.password != password:
            emit('error', {'msg': '房间密码错误'})
            return
        if room.state != 'lobby':
            emit('error', {'msg': '游戏已经开始，无法加入'})
            return
        if len(room.players) >= room.max_players:
            emit('error', {'msg': '房间已满（最多 %d 人）' % room.max_players})
            return
        pid = str(uuid.uuid4())
        with room.lock:
            room.join(pid, name, request.sid)
        emit('joined', {'room_id': rid, 'player_id': pid})
        broadcast(room)

    @socketio.on('rejoin', namespace=ns)
    def rejoin(data):
        data = data or {}
        rid = (data.get('room_id') or '').strip()
        pid = data.get('player_id')
        room = rooms.get(rid)
        if not room or pid not in room.players:
            emit('error', {'msg': '无法恢复房间，请重新加入'})
            return
        with room.lock:
            room.join(pid, room.players[pid]['name'], request.sid)
        emit('rejoined', {'room_id': rid, 'player_id': pid})
        broadcast(room)

    @socketio.on('add_bot', namespace=ns)
    def add_bot(data):
        """添加一个机器人玩家（仅等待阶段可用）。"""
        res = require_player(data)
        if not res:
            return
        room, pid, p = res
        with room.lock:
            if room.state != 'lobby':
                emit('error', {'msg': '游戏已开始，无法添加机器人'})
                return
            if len(room.players) >= room.max_players:
                emit('error', {'msg': '房间已满（最多 %d 人）' % room.max_players})
                return
            bot_pid = 'bot:' + uuid.uuid4().hex[:8]
            room.join(bot_pid, bot_display_name(room), None, bot=True)
        broadcast(room)

    @socketio.on('remove_bot', namespace=ns)
    def remove_bot(data):
        """移除一个机器人（仅房主，等待阶段）。"""
        res = require_player(data)
        if not res:
            return
        room, pid, p = res
        if pid != room.host_pid:
            emit('error', {'msg': '只有房主可以移除机器人'})
            return
        with room.lock:
            if room.state != 'lobby':
                emit('error', {'msg': '游戏已开始，无法移除机器人'})
                return
            bots = [q for q in room.players if room.is_bot(q)]
            if not bots:
                emit('error', {'msg': '没有可移除的机器人'})
                return
            q = bots[-1]
            removed_name = room.players[q]['name']
            room.players.pop(q, None)
            room.sid_map.pop(q, None)
            room.order.remove(q)
            room.log.append(f'{removed_name} 被移出房间')
        broadcast(room)

    @socketio.on('set_start_city', namespace=ns)
    def set_start_city(data):
        """房主在等待阶段修改出发城市（'random' 表示开局随机）。"""
        res = require_player(data)
        if not res:
            return
        room, pid, p = res
        with room.lock:
            try:
                room.set_start_city(pid, (data or {}).get('start_city') or '')
            except GameError as e:
                emit('error', {'msg': str(e)})
                return
            broadcast(room)

    @socketio.on('start_game', namespace=ns)
    def start_game(data):
        res = require_player(data)
        if not res:
            return
        room, pid, p = res
        with room.lock:
            try:
                room.start(pid)
            except GameError as e:
                emit('error', {'msg': str(e)})
                return
            broadcast(room)
            schedule_timer(room)
            maybe_bot_turn(room)

    @socketio.on('roll', namespace=ns)
    def roll(data):
        res = require_player(data)
        if not res:
            return
        room, pid, p = res
        with room.lock:
            try:
                room.roll(pid)
            except GameError as e:
                emit('error', {'msg': str(e)})
                return
            broadcast(room)

    @socketio.on('move', namespace=ns)
    def move(data):
        res = require_player(data)
        if not res:
            return
        room, pid, p = res
        neighbor = (data or {}).get('neighbor')
        with room.lock:
            try:
                room.move(pid, neighbor)
            except GameError as e:
                emit('error', {'msg': str(e)})
                return
            broadcast(room)
        # 走满步数后引擎会自动结束回合（phase 回到 roll），此时才给下一回合排程
        if room.state == 'playing' and room.phase == 'roll':
            schedule_timer(room)
            maybe_bot_turn(room)

    @socketio.on('answer_quiz', namespace=ns)
    def answer_quiz(data):
        """回答抵达城市时的知识题（地理 / 历史 / 文化）。"""
        res = require_player(data)
        if not res:
            return
        room, pid, p = res
        try:
            choice = int((data or {}).get('choice'))
        except (TypeError, ValueError):
            choice = -1
        with room.lock:
            try:
                detail = room.answer_quiz(pid, choice)
            except GameError as e:
                emit('error', {'msg': str(e)})
                return
            broadcast(room)
        # 把解析（正确答案 + 讲解）单独回给答题的人，方便弹窗展示
        emit('quiz_result', detail)

    @socketio.on('end_turn', namespace=ns)
    def end_turn(data):
        res = require_player(data)
        if not res:
            return
        room, pid, p = res
        with room.lock:
            try:
                room.end_turn(pid)
            except GameError as e:
                emit('error', {'msg': str(e)})
                return
            broadcast(room)
            if room.state == 'playing':
                schedule_timer(room)
                maybe_bot_turn(room)

    @socketio.on('chat', namespace=ns)
    def chat(data):
        """房间内聊天：只校验发言人在房间里，内容广播给全房间。
        返回值作为客户端 ack：True=已广播；False/无返回=未送达（前端据此提示）。"""
        res = require_player(data)
        if not res:
            return False
        room, pid, p = res
        with room.lock:
            if not room.add_chat(pid, (data or {}).get('text')):
                emit('error', {'msg': '消息内容为空'})
                return False
            broadcast(room)
        return True

    @socketio.on('disconnect', namespace=ns)
    def disconnect():
        sid = request.sid
        with rooms_lock:
            for room in list(rooms.values()):
                with room.lock:
                    for pid, p in list(room.players.items()):
                        if p['sid'] == sid and p['connected']:
                            room.mark_disconnected(pid)
                    broadcast(room)

    return {
        'rooms': rooms,
        'rooms_lock': rooms_lock,
        'namespace': ns,
        'broadcast': broadcast,
        'schedule_timer': schedule_timer,
        'maybe_bot_turn': maybe_bot_turn,
    }
