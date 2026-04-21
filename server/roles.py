"""角色模块：定义所有角色基类和具体角色实现（含行动逻辑）"""

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Literal, Optional, Type

from server.messages import RoleDict

if TYPE_CHECKING:
    from server.player import Player

logger = logging.getLogger(__name__)


# ──────────────────────────── 枚举 / 状态类 ────────────────────────────


class RolePhase(StrEnum):
    NIGHT = "night"
    DAY = "day"
    ON_DEATH = "on_death"  # 夜晚被杀或投票出局均触发（通配）
    ON_NIGHT_KILL = "on_night_kill"  # 仅夜晚被杀触发
    ON_VOTE_OUT = "on_vote_out"  # 仅投票出局触发


@dataclass
class NightState:
    kill_target: Optional[int] = None
    saved: Optional[int] = None
    poison_target: Optional[int] = None
    protected: Optional[int] = None


@dataclass
class RoleContext:
    """角色执行时的游戏上下文，解耦角色与 Game 对象"""

    players: list["Player"]  # 所有玩家（含死亡）
    night_state: NightState
    seer_results: dict[int, list]  # {seer_seat: [SeerResult, ...]}
    round: int = 0

    def get_alive_players(self) -> list["Player"]:
        return [p for p in self.players if p.is_alive]

    def get_player_by_seat(self, seat: int) -> "Player":
        player = next((p for p in self.players if p.seat == seat), None)
        if player is None:
            msg = f"未找到座位 {seat} 的玩家"
            raise RuntimeError(msg)
        return player


@dataclass
class ActionResult:
    success: bool
    message: str
    affected_seats: list[int] = field(default_factory=list)
    private: bool = True  # True=仅通知操作者，False=广播


# ──────────────────────────── 角色配置 ────────────────────────────


@dataclass
class RoleConfig:
    """从 YAML 读取的角色配置（行动相关字段）"""

    display_name: str
    phase: Optional[RolePhase]
    open_msg: Optional[str] = None
    close_msg: Optional[str] = None
    open_audio: Optional[list[str]] = None
    action_audio: Optional[list[str]] = None
    close_audio: Optional[list[str]] = None


# ──────────────────────────── 角色基类 ────────────────────────────


ROLE_REGISTRY: dict[str, Type["Role"]] = {}


class Role(ABC):
    """角色基类；子类声明 role_name= 时自动注册到 ROLE_REGISTRY"""

    name: str  # 由子类通过 role_name= 参数设置
    team: Literal["狼人", "村民", "神职", "中立"]  # 由子类定义

    def __init_subclass__(cls, role_name: str = "", **kwargs):
        super().__init_subclass__(**kwargs)
        if role_name:
            cls.name = role_name
            ROLE_REGISTRY[role_name] = cls

    def __init__(self, config: RoleConfig, raw: dict):
        self.display_name = config.display_name
        self.phase = config.phase
        self.open_msg = config.open_msg
        self.close_msg = config.close_msg
        self.open_audio = config.open_audio
        self.action_audio = config.action_audio
        self.close_audio = config.close_audio
        self.description: str = raw.get("description", "")
        # team 优先从 raw 读取，兜底用类属性
        self.team: Literal["狼人", "村民", "神职", "中立"] = raw.get("team", type(self).team)  # type: ignore[assignment]

    def can_use(self, player: "Player", ctx: RoleContext) -> bool:
        return player.is_alive

    def requires_target(self) -> bool:
        return True

    def can_skip(self) -> bool:
        return True

    @abstractmethod
    def get_valid_targets(self, player: "Player", ctx: RoleContext) -> list[int]:
        return [p.seat for p in ctx.get_alive_players() if p.seat != player.seat]

    @abstractmethod
    def execute(
        self, player: "Player", target_seat: Optional[int], ctx: RoleContext
    ) -> ActionResult:
        pass

    def to_dict(self) -> RoleDict:
        return {
            "name": self.name,
            "display_name": self.display_name,
            "team": self.team,
            "description": self.description,
            "phase": self.phase.value if self.phase else None,
            "can_skip": self.can_skip(),
        }


# ──────────────────────────── 具体角色实现 ────────────────────────────


class VillagerRole(Role, role_name="村民"):
    team = "村民"

    def get_valid_targets(self, player: "Player", ctx: RoleContext) -> list[int]:
        return []

    def execute(
        self, player: "Player", target_seat: Optional[int], ctx: RoleContext
    ) -> ActionResult:
        return ActionResult(True, "村民无行动")


class WerewolfRole(Role, role_name="狼人"):
    team = "狼人"

    def get_valid_targets(self, player: "Player", ctx: RoleContext) -> list[int]:
        return [p.seat for p in ctx.get_alive_players()]

    def execute(
        self, player: "Player", target_seat: Optional[int], ctx: RoleContext
    ) -> ActionResult:
        if target_seat is None:
            return ActionResult(True, "狼人选择放弃猎杀", private=False)
        target = ctx.get_player_by_seat(target_seat)
        ctx.night_state.kill_target = target_seat
        return ActionResult(
            True,
            f"狼人选择击杀 {target_seat} 号 {target.nickname}",
            affected_seats=[target_seat],
            private=False,
        )


class GuardRole(Role, role_name="守卫"):
    team = "神职"

    def __init__(self, config: RoleConfig, raw: dict):
        super().__init__(config, raw)
        self._last_protected: Optional[int] = None

    def get_valid_targets(self, player: "Player", ctx: RoleContext) -> list[int]:
        targets = [p.seat for p in ctx.get_alive_players()]
        if self._last_protected in targets:
            targets.remove(self._last_protected)
        return targets

    def execute(
        self, player: "Player", target_seat: Optional[int], ctx: RoleContext
    ) -> ActionResult:
        if not target_seat:
            return ActionResult(True, "守卫选择放弃守护", private=False)
        target = ctx.get_player_by_seat(target_seat)
        self._last_protected = target_seat
        ctx.night_state.protected = target_seat
        return ActionResult(True, f"守卫守护了 {target_seat} 号 {target.nickname}")


class WitchRole(Role, role_name="女巫"):
    team = "神职"

    def __init__(self, config: RoleConfig, raw: dict):
        super().__init__(config, raw)
        self.save_used = False
        self.poison_used = False

    def can_save(self, player: "Player", ctx: RoleContext) -> bool:
        return player.is_alive and not self.save_used and ctx.night_state.kill_target is not None

    def can_poison(self, player: "Player", ctx: RoleContext) -> bool:
        return player.is_alive and not self.poison_used

    def get_save_targets(self, player: "Player", ctx: RoleContext) -> list[int]:
        if self.save_used:
            return []
        # 女巫仅首夜能自救
        if ctx.night_state.kill_target == player.seat and ctx.round > 1:
            return []
        if ctx.night_state.kill_target is not None:
            return [ctx.night_state.kill_target]
        return []

    def get_poison_targets(self, player: "Player", ctx: RoleContext) -> list[int]:
        return [p.seat for p in ctx.get_alive_players() if p.seat != player.seat]

    def execute_save(self, player: "Player", target_seat: int, ctx: RoleContext) -> ActionResult:
        if self.save_used:
            return ActionResult(False, "解药已用完")
        target = ctx.get_player_by_seat(target_seat)
        self.save_used = True
        ctx.night_state.saved = target_seat
        return ActionResult(True, f"女巫使用解药救了 {target_seat} 号 {target.nickname}")

    def execute_poison(self, player: "Player", target_seat: int, ctx: RoleContext) -> ActionResult:
        if self.poison_used:
            return ActionResult(False, "毒药已用完")
        target = ctx.get_player_by_seat(target_seat)
        self.poison_used = True
        ctx.night_state.poison_target = target_seat
        return ActionResult(True, f"女巫毒死了 {target_seat} 号 {target.nickname}")

    # 满足抽象基类要求；实际通过 execute_save / execute_poison 调用
    def get_valid_targets(self, player: "Player", ctx: RoleContext) -> list[int]:
        return self.get_poison_targets(player, ctx)

    def execute(
        self, player: "Player", target_seat: Optional[int], ctx: RoleContext
    ) -> ActionResult:
        return ActionResult(False, "请使用 execute_save / execute_poison")


class SeerRole(Role, role_name="预言家"):
    team = "神职"

    def get_valid_targets(self, player: "Player", ctx: RoleContext) -> list[int]:
        return [p.seat for p in ctx.get_alive_players() if p.seat != player.seat]

    def execute(
        self, player: "Player", target_seat: Optional[int], ctx: RoleContext
    ) -> ActionResult:
        if not target_seat:
            raise ValueError("预言家查验必须指定目标")
        target = ctx.get_player_by_seat(target_seat)
        if not target.role:
            msg = f"目标 {target_seat} 没有角色信息"
            raise ValueError(msg)
        camp = "狼人" if target.role.team == "狼人" else "好人"
        return ActionResult(True, f"{target_seat} 号 {target.nickname} 是【{camp}】")


class HunterRole(Role, role_name="猎人"):
    team = "神职"

    def __init__(self, config: RoleConfig, raw: dict):
        super().__init__(config, raw)
        self._shot = False

    def can_use(self, player: "Player", ctx: RoleContext) -> bool:
        return not self._shot

    def get_valid_targets(self, player: "Player", ctx: RoleContext) -> list[int]:
        return [p.seat for p in ctx.get_alive_players() if p.seat != player.seat]

    def execute(
        self, player: "Player", target_seat: Optional[int], ctx: RoleContext
    ) -> ActionResult:
        if self._shot:
            return ActionResult(False, "已经开过枪了")
        if target_seat is None:
            return ActionResult(True, "猎人选择放弃开枪", private=False)
        target = ctx.get_player_by_seat(target_seat)
        self._shot = True
        return ActionResult(
            True,
            f"猎人开枪带走了 {target_seat} 号 {target.nickname}！",
            affected_seats=[target_seat],
            private=False,
        )


class IdiotRole(Role, role_name="白痴"):
    team = "神职"

    def __init__(self, config: RoleConfig, raw: dict):
        super().__init__(config, raw)
        self._revealed = False

    def can_use(self, player: "Player", ctx: RoleContext) -> bool:
        return not self._revealed

    def requires_target(self) -> bool:
        return False

    def can_skip(self) -> bool:
        return False

    def get_valid_targets(self, player: "Player", ctx: RoleContext) -> list[int]:
        return []

    def execute(
        self, player: "Player", target_seat: Optional[int], ctx: RoleContext
    ) -> ActionResult:
        self._revealed = True
        player.can_vote = False
        player.is_alive = True
        return ActionResult(
            True,
            f"白痴翻牌！{player.seat} 号 {player.nickname} 保留座位，失去投票权",
            private=False,
        )


# ──────────────────────────── 工厂函数 ────────────────────────────


def _to_list(v) -> Optional[list[str]]:
    if v is None:
        return None
    return v if isinstance(v, list) else [v]


def build_role_from_config(raw: dict) -> Role:
    """从 YAML role 配置字典构建 Role 实例（每次返回新实例，状态独立）"""
    name = raw["name"]
    cls = ROLE_REGISTRY.get(name)
    if cls is None:
        msg = f"未知角色: {name}"
        raise ValueError(msg)

    phase_raw = raw.get("phase")
    phase = RolePhase(phase_raw) if phase_raw else None

    config = RoleConfig(
        display_name=raw.get("display_name", name),
        phase=phase,
        open_msg=raw.get("open_msg"),
        close_msg=raw.get("close_msg"),
        open_audio=_to_list(raw.get("open_audio")),
        action_audio=_to_list(raw.get("action_audio")),
        close_audio=_to_list(raw.get("close_audio")),
    )
    return cls(config, raw)


def build_role_map(roles_config: list) -> dict[str, dict]:
    """将 YAML roles 列表转为 {role_name: raw_dict} 映射，供按需构建角色用"""
    return {r["name"]: r for r in roles_config}
