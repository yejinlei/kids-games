# -*- coding: utf-8 -*-
"""《世界之旅》：从中国走向全球的开放版旅游棋。

- 棋盘支持【平面地图】与【可旋转球面】两种视图，随时切换。
- 规则比《山河之旅》更开放：目标分、每回合步数都可由房主调整。
"""
from core.registry import GameSpec
from . import map_data

SPEC = GameSpec(
    id='shijie',
    name='世界之旅',
    category='棋类',
    icon='🌍',
    desc='从北京出发环游世界：跨洲航线、环球旅行，平面地图与可旋转地球两种视角自由切换。',
    rules=[
        '每回合掷 2 颗骰子，按颜色领取旅行票（GO 为万能票）',
        '沿航线移动，跨洲长线需要更多车票但分值更高',
        '停在与桌面城市卡相同的城市即可拿走该卡得分（途经不算）',
        '达到目标分并停在秘密目的地上，立刻获胜',
        '开放规则：房主可调整目标分（10/15/20）与每回合步数（1-3 步）',
    ],
    tags=['儿童', '世界地理', '2-4 人', '开放规则'],
    max_players=4,
    views=['flat', 'globe'],
    options={'win_score': [10, 15, 20], 'steps_per_turn': [1, 2, 3]},
    map=map_data.build_map(),
)
