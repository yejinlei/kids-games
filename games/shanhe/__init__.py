# -*- coding: utf-8 -*-
"""《山河之旅》：正版还原的中国地理旅游棋。"""
from core.registry import GameSpec
from . import map_data

SPEC = GameSpec(
    id='shanhe',
    name='山河之旅',
    category='棋类',
    icon='🏔',
    desc='从北京出发，掷骰积攒五色旅行票，沿线路游遍中国，先抵达秘密目的地者获胜。',
    rules=[
        '每回合掷 2 颗骰子，按颜色领取旅行票（GO 是万能票：能和同色票凑着用，缺几张抵几张）',
        '每回合只走 1 条线路，付清票价即可：优先花同色票，不够的部分用 GO 抵',
        '停在与桌面城市卡相同的城市即可拿走该卡得分（途经不算）',
        '累计 10 分并停在秘密目的地上，立刻获胜',
    ],
    tags=['儿童', '地理启蒙', '2-4 人'],
    max_players=4,
    views=['flat'],
    # 严格对齐正版，规则不做开放调整
    options={},
    map=map_data.build_map(),
)
