# -*- coding: utf-8 -*-
"""平台部署入口（WSGI 应用对象）。

各家云平台（Fly.io / Render / Railway）的 Python 构建包默认按 Flask 约定启动：
自动查找 app.py / wsgi.py 中的 `app` 对象，或用 `gunicorn app:app` 启动。
这里显式暴露 `app`，使默认的构建包流程无需额外配置即可运行。

注意：导入 server 时 Flask-SocketIO 已完成初始化（threading 模式会包装
app.wsgi_app），因此 Socket.IO 请求同样能被处理，多人联机不受影响。
"""
import os

from server import app, socketio  # noqa: F401

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 8080))
    socketio.run(app, host='0.0.0.0', port=port, allow_unsafe_werkzeug=True)
