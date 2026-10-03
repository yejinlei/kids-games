# -*- coding: utf-8 -*-
"""游戏元数据：大厅按分类展示用。"""


class GameSpec:
    def __init__(self, id, name, category, desc='', icon='🎲', rules=None,
                 max_players=4, map=None, tags=None, views=None, options=None):
        self.id = id                    # 英文短名，用作命名空间 /<id> 与路径 /g/<id>/
        self.name = name                # 中文名
        self.category = category        # 分类：棋类 / 牌类 / ...
        self.desc = desc or ''
        self.icon = icon
        self.rules = list(rules or [])
        self.max_players = max_players
        self.map = map
        self.tags = list(tags or [])
        # 可用视图：flat=平面地图，globe=可旋转球面（需地图提供经纬度）
        self.views = list(views or ['flat'])
        # 房主可选规则（键为规则名，值为候选值列表）
        self.options = dict(options or {})

    def to_json(self):
        return {
            'id': self.id,
            'name': self.name,
            'category': self.category,
            'desc': self.desc,
            'icon': self.icon,
            'rules': self.rules,
            'max_players': self.max_players,
            'tags': self.tags,
            'cities': len(self.map.CITY_GEO) if self.map else 0,
            'routes': len(self.map.ROUTES) if self.map else 0,
            'views': self.views,
            'options': self.options,
        }
