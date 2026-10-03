# -*- coding: utf-8 -*-
"""《山河之旅》后端服务。
- Flask 提供静态页面与地图接口
- Flask-SocketIO 负责房间、实时状态同步与回合超时控制
"""
import os
import random
import re
import threading
import time
import uuid

from flask import Flask, send_from_directory, jsonify, request
from flask_socketio import SocketIO, emit

from engine import GameRoom, GameError
import map_data
import ai

app = Flask(__name__, static_folder='static')
socketio = SocketIO(app, cors_allowed_origins='*', async_mode='threading')

rooms = {}
rooms_lock = threading.RLock()


# -------------------- HTTP --------------------
@app.after_request
def add_cors(resp):
    """允许前端跨域直连（前端页面与后端不同源时）。"""
    resp.headers.setdefault('Access-Control-Allow-Origin', '*')
    resp.headers.setdefault('Access-Control-Allow-Headers', 'Content-Type')
    resp.headers.setdefault('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
    return resp


@app.route('/')
def index():
    return send_from_directory('static', 'index.html')


@app.route('/api/map')
def api_map():
    return jsonify({
        'cities': {c: {'points': map_data.CITY_POINTS[c]} for c in map_data.CITY_GEO},
        'geo': {c: list(map_data.CITY_GEO[c]) for c in map_data.CITY_GEO},
        'border': [list(p) for p in map_data.CHINA_BORDER],
        'routes': [list(r) for r in map_data.ROUTES],
        'colors': map_data.COLORS,
        'wild': map_data.WILD,
        'dice_faces': map_data.DICE_FACES,
        'win_score': 10,
    })


@app.route('/api/rooms')
def api_rooms():
    """大厅房间列表：等待中且未满的房间。"""
    out = []
    with rooms_lock:
        for rid, room in rooms.items():
            with room.lock:
                if room.state != 'lobby' or len(room.players) >= room.max_players:
                    continue
                humans = sum(1 for p in room.players.values() if not p['bot'])
                bots = sum(1 for p in room.players.values() if p['bot'])
                out.append({
                    'room_id': rid,
                    'creator': room.creator_name or '房主',
                    'has_password': bool(room.password),
                    'humans': humans,
                    'bots': bots,
                    'current': len(room.players),
                    'max_players': room.max_players,
                    'dice_count': room.dice_count,
                    'timeout': room.timeout,
                })
    out.sort(key=lambda r: r['room_id'])
    return jsonify({'rooms': out})


# -------------------- 工具 --------------------
def gen_room_id():
    while True:
        rid = ''.join(random.choices('0123456789', k=4))
        if rid not in rooms:
            return rid


def get_room(data):
    rid = (data or {}).get('room_id')
    return rooms.get(rid)


def broadcast(room):
    with room.lock:
        for pid, p in list(room.players.items()):
            sid = room.sid_map.get(pid)
            if not sid or not p['connected']:
                continue
            socketio.emit('state', room.serialize(pid), to=sid)


BOT_NAMES = ['🤖小旅', '🤖山河', '🤖骆驼', '🤖指南针']


def bot_display_name(room):
    """给机器人起个不重复的名字。"""
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
    """机器人一个完整回合：掷骰 -> 逐步移动 -> 结束回合。"""
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

    for _ in range(15):     # 最多尝试若干步；官方每回合只能走 1 步，走完即自动结束
        with room.lock:
            if room.state != 'playing' or room.current_pid != pid or room.phase != 'move':
                return
            city = ai.next_step(room, pid, room.ai_level)
            if not city:
                break
            try:
                room.move(pid, city)
            except GameError:
                break
            broadcast(room)
        time.sleep(0.6)

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
    """返回 (room, pid, player) 或抛出异常时返回 None。"""
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
@socketio.on('create_room')
def create_room(data):
    data = data or {}
    name = (data.get('name') or '玩家').strip()[:12] or '玩家'
    password = (data.get('password') or '').strip()
    dice_count = max(1, int(data.get('dice_count', 2) or 2))
    timeout = int(data.get('timeout', 30) or 30)
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
        room = GameRoom(rid, password, dice_count, timeout, ai_level=ai_level, max_players=max_players)
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


@socketio.on('join_room')
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
    if len(room.players) >= 4:
        emit('error', {'msg': '房间已满（最多 4 人）'})
        return
    pid = str(uuid.uuid4())
    with room.lock:
        room.join(pid, name, request.sid)
    emit('joined', {'room_id': rid, 'player_id': pid})
    broadcast(room)


@socketio.on('rejoin')
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


@socketio.on('add_bot')
def add_bot(data):
    """添加一个机器人玩家（仅等待阶段可用，最多 4 人）。"""
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
        name = bot_display_name(room)
        room.join(bot_pid, name, None, bot=True)
    broadcast(room)


@socketio.on('remove_bot')
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


@socketio.on('start_game')
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


@socketio.on('roll')
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


@socketio.on('move')
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
    # 移动一次后引擎已自动结束本回合：为下一回合排程定时器并触发可能的机器人
    if room.state == 'playing':
        schedule_timer(room)
        maybe_bot_turn(room)


@socketio.on('end_turn')
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


@socketio.on('disconnect')
def disconnect():
    sid = request.sid
    with rooms_lock:
        for room in list(rooms.values()):
            with room.lock:
                for pid, p in list(room.players.items()):
                    if p['sid'] == sid and p['connected']:
                        room.mark_disconnected(pid)
                broadcast(room)


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    print(f'山河之旅 服务已启动: http://localhost:{port}')
    socketio.run(app, host='0.0.0.0', port=port, allow_unsafe_werkzeug=True)
