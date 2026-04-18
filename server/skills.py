"""技能模块：定义所有技能基类和具体技能实现"""

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Optional, Type

from server.messages import SkillDict

if TYPE_CHECKING:
    from server.player import Player  # noqa: TC004

logger = logging.getLogger(__name__)


class SkillPhase(StrEnum):
    NIGHT = "night"
    DAY = "day"
    ON_DEATH = "on_death"


@dataclass
class NightState:
    kill_target: Optional[int] = None
    saved: Optional[int] = None
    poison_target: Optional[int] = None
    protected: Optional[int] = None


@dataclass
class SkillContext:
    """技能执行时的游戏上下文，解耦技能与 Game 对象"""

    players: list[Player]  # 所有玩家（含死亡）
    night_state: NightState
    seer_results: dict[int, list]  # {seer_seat: [SeerResult, ...]}
    round: int = 0

    def get_alive_players(self) -> list[Player]:
        return [p for p in self.players if p.is_alive]

    def get_player_by_seat(self, seat: int) -> Player:
        player = next((p for p in self.players if p.seat == seat), None)
        if player is None:
            msg = f"未找到座位 {seat} 的玩家"
            raise RuntimeError(msg)
        return player


@dataclass
class SkillResult:
    success: bool
    message: str
    affected_seats: list[int] = field(default_factory=list)
    private: bool = True  # True=仅通知操作者，False=广播


@dataclass
class SkillConfig:
    """从 YAML 读取的技能配置"""

    display_name: str
    priority: int
    phase: SkillPhase
    open_msg: Optional[str] = None
    close_msg: Optional[str] = None
    open_audio: Optional[list[str]] = None
    action_audio: Optional[list[str]] = None
    close_audio: Optional[list[str]] = None


SKILL_REGISTRY: dict[str, Type["Skill"]] = {}


class Skill(ABC):
    """技能基类；子类声明 skill_name= 时自动注册到 SKILL_REGISTRY"""

    name: str  # 由子类定义时的 skill_name= 参数设置

    def __init_subclass__(cls, skill_name: str = "", **kwargs):
        super().__init_subclass__(**kwargs)
        if skill_name:
            cls.name = skill_name
            SKILL_REGISTRY[skill_name] = cls

    def __init__(self, config: SkillConfig):
        self.display_name = config.display_name
        self.phase = config.phase
        self.priority = config.priority
        self.open_msg = config.open_msg
        self.close_msg = config.close_msg
        self.open_audio = config.open_audio
        self.action_audio = config.action_audio
        self.close_audio = config.close_audio

    def can_use(self, player: Player, ctx: SkillContext) -> bool:
        return player.is_alive

    def requires_target(self) -> bool:
        return True

    def can_skip(self) -> bool:
        return True

    @abstractmethod
    def get_valid_targets(self, player: Player, ctx: SkillContext) -> list[int]:
        """返回有效目标列表，None 表示技能不可用，[] 表示无目标（自动执行）"""
        return [p.seat for p in ctx.get_alive_players() if p.seat != player.seat]

    @abstractmethod
    def execute(self, player: Player, target_seat: Optional[int], ctx: SkillContext) -> SkillResult:
        pass

    def to_dict(self) -> SkillDict:
        return {
            "name": self.name,
            "display_name": self.display_name,
            "phase": self.phase.value,
            "priority": self.priority,
            "can_skip": self.can_skip(),
        }


# ──────────────────────────── 具体技能 ────────────────────────────


class WerewolfKill(Skill, skill_name="狼人猎杀"):
    def get_valid_targets(self, player: Player, ctx: SkillContext) -> list[int]:
        return [p.seat for p in ctx.get_alive_players() if p.is_alive]

    def execute(self, player: Player, target_seat: Optional[int], ctx: SkillContext) -> SkillResult:
        if target_seat is None:
            return SkillResult(True, "狼人选择放弃猎杀", private=False)
        target = ctx.get_player_by_seat(target_seat)
        ctx.night_state.kill_target = target_seat
        return SkillResult(
            True,
            f"狼人选择击杀 {target_seat} 号 {target.nickname}",
            affected_seats=[target_seat],
            private=False,
        )


class GuardProtect(Skill, skill_name="守卫守护"):
    def __init__(self, config: SkillConfig):
        super().__init__(config)
        self._last_protected: Optional[int] = None

    def get_valid_targets(self, player: Player, ctx: SkillContext) -> list[int]:
        targets = [p.seat for p in ctx.get_alive_players()]
        if self._last_protected in targets:
            targets.remove(self._last_protected)
        return targets

    def execute(self, player: Player, target_seat: Optional[int], ctx: SkillContext) -> SkillResult:
        if not target_seat:
            return SkillResult(True, "守卫选择放弃守护", private=False)

        target = ctx.get_player_by_seat(target_seat)
        self._last_protected = target_seat
        ctx.night_state.protected = target_seat
        return SkillResult(True, f"守卫守护了 {target_seat} 号 {target.nickname}")


class WitchSave(Skill, skill_name="女巫解药"):
    def __init__(self, config: SkillConfig):
        super().__init__(config)
        self._used = False

    def can_use(self, player: Player, ctx: SkillContext) -> bool:
        return player.is_alive and not self._used and ctx.night_state.kill_target is not None

    def get_valid_targets(self, player: Player, ctx: SkillContext) -> list[int]:
        if self._used:
            return []
        # 女巫仅首夜能自救
        if ctx.night_state.kill_target == player.seat and ctx.round > 1:
            return []
        if ctx.night_state.kill_target is not None:
            return [ctx.night_state.kill_target]
        return []

    def execute(self, player: Player, target_seat: Optional[int], ctx: SkillContext) -> SkillResult:
        if self._used:
            return SkillResult(False, "解药已用完")
        if not target_seat:
            return SkillResult(True, "女巫选择放弃使用解药")
        target = ctx.get_player_by_seat(target_seat)
        self._used = True
        ctx.night_state.saved = target_seat
        return SkillResult(True, f"女巫使用解药救了 {target_seat} 号 {target.nickname}")


class WitchPoison(Skill, skill_name="女巫毒药"):
    def __init__(self, config: SkillConfig):
        super().__init__(config)
        self._used = False

    def can_use(self, player: Player, ctx: SkillContext) -> bool:
        return player.is_alive and not self._used

    def get_valid_targets(self, player: Player, ctx: SkillContext) -> list[int]:
        return [p.seat for p in ctx.get_alive_players() if p.seat != player.seat]

    def execute(self, player: Player, target_seat: Optional[int], ctx: SkillContext) -> SkillResult:
        if self._used:
            return SkillResult(False, "毒药已用完")
        if not target_seat:
            return SkillResult(True, "女巫选择放弃使用毒药")
        target = ctx.get_player_by_seat(target_seat)
        self._used = True
        ctx.night_state.poison_target = target_seat
        return SkillResult(True, f"女巫毒死了 {target_seat} 号 {target.nickname}")


class SeerCheck(Skill, skill_name="预言家查验"):
    def get_valid_targets(self, player: "Player", ctx: SkillContext) -> list[int]:
        return [p.seat for p in ctx.get_alive_players() if p.seat != player.seat]

    def execute(self, player: Player, target_seat: Optional[int], ctx: SkillContext) -> SkillResult:
        if not target_seat:
            raise ValueError("预言家查验必须指定目标")
        target = ctx.get_player_by_seat(target_seat)
        if not target.role:
            msg = f"目标 {target_seat} 没有角色信息"
            raise ValueError(msg)
        camp = "狼人" if target.role.team == "狼人" else "好人"
        return SkillResult(True, f"{target_seat} 号 {target.nickname} 是【{camp}】")


class HunterShoot(Skill, skill_name="猎人开枪"):
    def __init__(self, config: SkillConfig):
        super().__init__(config)
        self._shot = False

    def can_use(self, player: Player, ctx: SkillContext) -> bool:
        return not self._shot

    def get_valid_targets(self, player: Player, ctx: SkillContext) -> list[int]:
        return super().get_valid_targets(player, ctx)

    def execute(self, player: Player, target_seat: Optional[int], ctx: SkillContext) -> SkillResult:
        if self._shot:
            return SkillResult(False, "已经开过枪了")
        if target_seat is None:
            return SkillResult(True, "猎人选择放弃开枪", private=False)

        target = ctx.get_player_by_seat(target_seat)
        self._shot = True
        return SkillResult(
            True,
            f"猎人开枪带走了 {target_seat} 号 {target.nickname}！",
            affected_seats=[target_seat],
            private=False,
        )


class IdiotReveal(Skill, skill_name="白痴翻牌"):
    def __init__(self, config: SkillConfig):
        super().__init__(config)
        self._revealed = False

    def can_use(self, player: Player, ctx: SkillContext) -> bool:
        return not self._revealed

    def requires_target(self) -> bool:
        return False

    def can_skip(self) -> bool:
        return False

    def get_valid_targets(self, player: Player, ctx: SkillContext) -> list[int]:
        return []

    def execute(self, player: Player, target_seat: Optional[int], ctx: SkillContext) -> SkillResult:
        self._revealed = True
        player.can_vote = False
        player.is_alive = True
        return SkillResult(
            True,
            f"白痴翻牌！{player.seat} 号 {player.nickname} 保留座位，失去投票权",
            private=False,
        )


def build_skill_from_config(raw: dict) -> Skill:
    """从 YAML skill 配置字典构建 Skill 实例"""
    name = raw["name"]
    cls = SKILL_REGISTRY.get(name)
    if cls is None:
        msg = f"未知技能: {name}"
        raise ValueError(msg)

    phase_raw = raw.get("phase", "night")
    phase = SkillPhase(phase_raw)

    def _to_list(v) -> Optional[list[str]]:
        if v is None:
            return None
        return v if isinstance(v, list) else [v]

    config = SkillConfig(
        display_name=raw.get("display_name", name),
        priority=raw.get("priority", 99),
        phase=phase,
        open_msg=raw.get("open_msg"),
        close_msg=raw.get("close_msg"),
        open_audio=_to_list(raw.get("open_audio")),
        action_audio=_to_list(raw.get("action_audio")),
        close_audio=_to_list(raw.get("close_audio")),
    )
    return cls(config)
