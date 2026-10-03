# -*- coding: utf-8 -*-
"""通用旅游棋"地图数据"容器。

一张地图 = 城市(经纬度) + 线路(两端/颜色/票数) + 陆地轮廓(经纬度多边形)。
同一份数据同时供：
- core.journey.JourneyRoom（游戏引擎）
- core.ai（机器人寻路）
- HTTP 地图接口（已投影好的画布坐标，前端直接渲染，无需再算投影）

这样新增一款棋只需写一份 map_data.py，引擎/AI/前端全部复用。
"""
from collections import deque

import math

# 画布尺寸（所有棋盘统一，前端按 viewBox 等比缩放）
CANVAS_W = 1000
CANVAS_H = 720
CANVAS_PAD = 55

# 车票/路线颜色（含中文名与展示色）。正版五色：红、紫、绿、蓝、黄
COLORS = {
    'red':    {'hex': '#e74c3c', 'name': '红'},
    'blue':   {'hex': '#3498db', 'name': '蓝'},
    'green':  {'hex': '#2ecc71', 'name': '绿'},
    'yellow': {'hex': '#f1c40f', 'name': '黄'},
    'purple': {'hex': '#8e44ad', 'name': '紫'},
}

# 万能车票（GO 骰）：可作为任意颜色使用
WILD = 'go'

# 骰子六面：五种颜色 + 万能 GO（必须覆盖所有路线颜色）
DICE_FACES = ['red', 'blue', 'green', 'yellow', 'purple', 'go']

# 官方规则：每回合固定掷 2 颗骰子
DICE_PER_TURN = 2


def china_projection(lon, lat):
    """《山河之旅》等距投影：按北纬 35° 收窄经度，贴合中国版图形状。"""
    k = math.cos(math.radians(35))
    return ((lon - 71) * k, 54 - lat)


def world_projection(lon, lat):
    """《世界之旅》等距圆柱（Plate Carrée）投影：全球通用，y 轴翻转使北在上。"""
    return (lon, -lat)


class GameMap:
    def __init__(self, name='', city_geo=None, city_points=None, routes=None,
                 colors=None, wild=WILD, dice_faces=None, start_city=None,
                 land=None, dice_per_turn=DICE_PER_TURN, win_score=10,
                 projection=None, canvas_w=CANVAS_W, canvas_h=CANVAS_H,
                 pad=CANVAS_PAD, city_info=None, features=None,
                 medal_cities=3, medal_score=2, shared_cards=3):
        self.name = name
        # 桌面翻开的城市卡数量：正版为 3 张；城市很多的大棋盘需要多翻几张，否则得分太慢
        self.SHARED_N = max(1, int(shared_cards))
        # city_info：城市知识卡（地理 / 历史 / 风土 / 问答题），旅游学习用
        self.CITY_INFO = dict(city_info or {})
        # features：可选玩法开关（culture=知识卡问答, passport=护照印章, medal=大洲奖章）
        self.FEATURES = set(features or [])
        self.MEDAL_CITIES = max(2, int(medal_cities))
        self.MEDAL_SCORE = max(1, int(medal_score))
        self.CITY_GEO = dict(city_geo or {})
        self.CITY_POINTS = dict(city_points or {})
        self.ROUTES = list(routes or [])
        self.COLORS = dict(colors or {})
        self.WILD = wild
        self.DICE_FACES = list(dice_faces or [])
        self.DICE_PER_TURN = dice_per_turn
        self.WIN_SCORE = win_score
        self.LAND = [list(p) for p in (land or [])]   # 每个元素：[[lon,lat], ...]
        self.START_CITY = start_city or next(iter(self.CITY_GEO), None)
        self.projection = projection or world_projection
        self.canvas_w, self.canvas_h, self.pad = canvas_w, canvas_h, pad

        # 邻接表：(邻居, 颜色, 票数)，忽略颜色用于 AI 估算距离
        self.ADJ = {c: [] for c in self.CITY_GEO}
        for a, b, color, cost in self.ROUTES:
            self.ADJ[a].append((b, color, cost))
            self.ADJ[b].append((a, color, cost))

        # 全源最短路（忽略颜色与票数，只数"段数"），供 AI 查表
        self.ALL_DIST = {c: self._bfs(c) for c in self.CITY_GEO}

    # ---------- 图算法 ----------
    def _bfs(self, start):
        dist = {start: 0}
        q = deque([start])
        while q:
            n = q.popleft()
            for m, _c, _w in self.ADJ[n]:
                if m not in dist:
                    dist[m] = dist[n] + 1
                    q.append(m)
        return dist

    # ---------- 投影 ----------
    def project(self):
        """把经纬度投影并等比缩放进画布，返回 (城市坐标, 陆地轮廓坐标)。"""
        W, H, pad = self.canvas_w, self.canvas_h, self.pad
        raw = self.projection
        pts = [raw(v[0], v[1]) for v in self.CITY_GEO.values()]
        for poly in self.LAND:
            pts += [raw(p[0], p[1]) for p in poly]
        if not pts:
            return {}, []
        min_x = min(p[0] for p in pts)
        max_x = max(p[0] for p in pts)
        min_y = min(p[1] for p in pts)
        max_y = max(p[1] for p in pts)
        sx = (W - 2 * pad) / max(max_x - min_x, 1e-6)
        sy = (H - 2 * pad) / max(max_y - min_y, 1e-6)
        s = min(sx, sy)
        off_x = pad + ((W - 2 * pad) - (max_x - min_x) * s) / 2
        off_y = pad + ((H - 2 * pad) - (max_y - min_y) * s) / 2

        def to_xy(lon, lat):
            rx, ry = raw(lon, lat)
            return {'x': round(off_x + (rx - min_x) * s, 1),
                    'y': round(off_y + (ry - min_y) * s, 1)}

        cities = {}
        for c, (lon, lat) in self.CITY_GEO.items():
            cities[c] = to_xy(lon, lat)
        land = [[[round(to_xy(p[0], p[1])['x'], 1), round(to_xy(p[0], p[1])['y'], 1)]
                 for p in poly] for poly in self.LAND]
        return cities, land

    def to_json(self):
        """地图接口数据。

        - cities / land：已投影好的画布坐标，供【平面视图】直接渲染
        - geo / land_geo：原始经纬度，供【球面视图】做三维投影与旋转
        """
        pos, land = self.project()
        return {
            'name': self.name,
            'start': self.START_CITY,
            'geo': {c: list(v) for c, v in self.CITY_GEO.items()},
            'land_geo': [[[p[0], p[1]] for p in poly] for poly in self.LAND],
            'cities': [{'name': c, 'x': pos[c]['x'], 'y': pos[c]['y'],
                        'points': self.CITY_POINTS.get(c, 0)} for c in self.CITY_GEO],
            'land': land,
            'routes': [list(r) for r in self.ROUTES],
            'colors': self.COLORS,
            'wild': self.WILD,
            'dice_faces': self.DICE_FACES,
            'dice_per_turn': self.DICE_PER_TURN,
            'win_score': self.WIN_SCORE,
            'city_info': self.CITY_INFO,
            'features': sorted(self.FEATURES),
            'medal_cities': self.MEDAL_CITIES,
            'medal_score': self.MEDAL_SCORE,
            'shared_n': self.SHARED_N,
        }

    # ---------- 数据自检 ----------
    def validate(self):
        """返回问题列表（空列表 = 数据健康）。用于测试与新增棋盘时自检。"""
        errs = []
        faces = set(self.DICE_FACES)
        for a, b, color, cost in self.ROUTES:
            if a not in self.CITY_GEO or b not in self.CITY_GEO:
                errs.append('线路两端城市不存在: %s-%s' % (a, b))
            if color not in self.COLORS:
                errs.append('线路颜色非法: %s-%s %s' % (a, b, color))
            if color not in faces:
                errs.append('颜色 %s 不在骰面内，该色线路永远无法通行' % color)
            if not isinstance(cost, int) or cost < 1:
                errs.append('票数非法: %s-%s %s' % (a, b, cost))
        seen = set()
        for a, b, _c, _w in self.ROUTES:
            key = tuple(sorted((a, b)))
            if key in seen:
                errs.append('重复线路: %s-%s' % (a, b))
            seen.add(key)
        if self.START_CITY not in self.CITY_GEO:
            errs.append('出发城市不在城市中: %s' % self.START_CITY)
        reach = self.ALL_DIST.get(self.START_CITY, {})
        for c in self.CITY_GEO:
            if c not in reach:
                errs.append('孤城（从出发地不可达）: %s' % c)
            if c not in self.CITY_POINTS:
                errs.append('缺少分值: %s' % c)
        return errs
