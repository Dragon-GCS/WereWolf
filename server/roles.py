"""角色模块：从配置构建角色实例"""

from dataclasses import dataclass
from typing import Literal

from server.messages import RoleDict

from .skills import Skill, build_skill_from_config


@dataclass
class Role:
    name: str
    display_name: str
    team: Literal["狼人", "村民", "神职", "中立"]
    skills: list[Skill]
    description: str = ""

    def to_dict(self) -> RoleDict:
        return {
            "name": self.name,
            "display_name": self.display_name,
            "team": self.team,
            "description": self.description,
            "skills": [s.to_dict() for s in self.skills],
        }


def build_role_from_config(raw: dict) -> Role:
    """从 YAML role 配置字典构建 Role 实例（每次返回新实例，技能状态独立）"""
    if "skills" not in raw or not isinstance(raw["skills"], list):
        msg = f"角色配置缺少技能列表或格式错误: {raw}"
        raise ValueError(msg)
    skills = [build_skill_from_config(s) for s in raw["skills"]]
    return Role(
        name=raw["name"],
        display_name=raw.get("display_name", raw["name"]),
        team=raw["team"],
        skills=skills,
        description=raw.get("description", ""),
    )


def build_role_map(roles_config: list) -> dict[str, dict]:
    """将 YAML roles 列表转为 {role_name: raw_dict} 映射，供按需构建角色用"""
    return {r["name"]: r for r in roles_config}
