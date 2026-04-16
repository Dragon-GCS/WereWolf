"""角色模块：定义角色类及角色工厂"""

import logging
from typing import List

from .skills import (
    GuardProtect,
    HunterShoot,
    IdiotReveal,
    SeerCheck,
    Skill,
    WerewolfKill,
    WitchPoison,
    WitchSave,
)

logger = logging.getLogger(__name__)


class Role:
    """角色类"""

    def __init__(
        self,
        name: str,
        display_name: str,
        team: str,
        skills: List[Skill],
        description: str = "",
    ):
        self.name = name
        self.display_name = display_name
        self.team = team  # "werewolf" | "villager" | "neutral"
        self.skills = skills
        self.description = description

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "display_name": self.display_name,
            "team": self.team,
            "description": self.description,
            "skills": [s.to_dict() for s in self.skills],
        }


# ──────────────────────────── 角色工厂 ────────────────────────────

_ROLE_FACTORIES = {
    "werewolf": lambda: Role(
        "werewolf",
        "狼人",
        "werewolf",
        [WerewolfKill()],
        "夜晚与狼队共同猎杀一名好人",
    ),
    "villager": lambda: Role(
        "villager",
        "村民",
        "villager",
        [],
        "没有特殊技能，通过推理找出狼人",
    ),
    "seer": lambda: Role(
        "seer",
        "预言家",
        "villager",
        [SeerCheck()],
        "每晚可查验一名玩家的阵营",
    ),
    "witch": lambda: Role(
        "witch",
        "女巫",
        "villager",
        [WitchSave(), WitchPoison()],
        "持有解药和毒药各一瓶，可在夜晚使用",
    ),
    "hunter": lambda: Role(
        "hunter",
        "猎人",
        "villager",
        [HunterShoot()],
        "被淘汰时可以开枪带走一名玩家",
    ),
    "guard": lambda: Role(
        "guard",
        "守卫",
        "villager",
        [GuardProtect()],
        "每晚守护一名玩家，不能连续守护同一人",
    ),
    "idiot": lambda: Role(
        "idiot",
        "白痴",
        "villager",
        [IdiotReveal()],
        "被投票出局时翻牌，保留座位但失去投票权",
    ),
}


def create_role(role_name: str) -> Role:
    """根据角色名称创建角色实例"""
    factory = _ROLE_FACTORIES.get(role_name)
    if factory is None:
        raise ValueError(f"未知角色: {role_name}")
    return factory()


def get_all_role_names() -> List[str]:
    return list(_ROLE_FACTORIES.keys())
