# -*- coding: utf-8 -*-
"""儿童益智类游戏大厅服务端：一个入口托管多款棋牌。

- `/`                大厅（按分类浏览）
- `/g/<game_id>/`    具体某一款棋的页面（共用同一个客户端）
- `/api/games`       游戏与分类列表
- Socket.IO 每款棋一个命名空间 `/<game_id>`，房间互不干扰

新增一款棋：在 games/ 下建目录（写 map_data.py + __init__.py 注册 GameSpec），
再在 games/__init__.py 里加一行即可，无需改动本文件。
"""
import os

from flask import Flask, abort, jsonify, send_from_directory
from flask_socketio import SocketIO

from core.net import register_game
from games import GAMES, categories

app = Flask(__name__, static_folder='static')
socketio = SocketIO(app, cors_allowed_origins='*', async_mode='threading')

SPECS = {s.id: s for s in GAMES}


@app.after_request
def add_cors(resp):
    """允许前端跨域直连（前端页面与后端不同源时）。"""
    resp.headers.setdefault('Access-Control-Allow-Origin', '*')
    resp.headers.setdefault('Access-Control-Allow-Headers', 'Content-Type')
    resp.headers.setdefault('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
    return resp


@app.route('/')
def lobby():
    return send_from_directory('static/lobby', 'index.html')


@app.route('/g/<gid>/')
def game_page(gid):
    if gid not in SPECS:
        abort(404)
    return send_from_directory('static', 'game.html')


@app.route('/api/games')
def api_games():
    return jsonify({'games': [s.to_json() for s in GAMES],
                    'categories': categories()})


for _spec in GAMES:
    register_game(app, socketio, _spec)


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    print('儿童益智类游戏大厅已启动: http://localhost:%d' % port)
    print('已载入游戏: ' + '、'.join('%s(/g/%s/)' % (s.name, s.id) for s in GAMES))
    socketio.run(app, host='0.0.0.0', port=port, allow_unsafe_werkzeug=True)
