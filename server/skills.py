"""技能模块：定义所有技能基类和具体技能实现"""

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, List, Optional

if TYPE_CHECKING:
    from .game import Game
    from .player import Player

logger = logging.getLogger(__name__)


class SkillPhase(str, Enum):
    NIGHT = "night"
    DAY = "day"
    ON_DEATH = "on_death"


@dataclass
class SkillResult:
    success: bool
    message: str
    affected_seats: List[int] = field(default_factory=list)
    private: bool = True  # 仅通知操作者；False 则广播


class Skill(ABC):
    """技能基类"""

    def __init__(
        self,
        name: str,
        display_name: str,
        phase: SkillPhase,
        priority: int,
        description: str = "",
    ):
        self.name = name
        self.display_name = display_name
        self.phase = phase
        self.priority = priority
        self.description = description

    def can_use(self, player: "Player", game: "Game") -> bool:
        """技能是否可用"""
        return player.is_alive

    def requires_target(self) -> bool:
        """是否需要选择目标"""
        return True

    def can_skip(self) -> bool:
        """是否可以跳过"""
        return True

    def get_valid_targets(self, player: "Player", game: "Game") -> List[int]:
        """返回有效目标的座位号列表"""
        return [p.seat for p in game.get_alive_players() if p.seat != player.seat]

    @abstractmethod
    def execute(self, player: "Player", target_seat: Optional[int], game: "Game") -> SkillResult:
        pass

    def reset_round(self) -> None:
        """每轮重置（如有需要则覆盖）"""
        pass

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "display_name": self.display_name,
            "phase": self.phase.value,
            "priority": self.priority,
            "description": self.description,
            "can_skip": self.can_skip(),
        }


# ──────────────────────────── 具体技能 ────────────────────────────


class WerewolfKill(Skill):
    """狼人猎杀"""

    def __init__(self):
        super().__init__("werewolf_kill", "猎杀", SkillPhase.NIGHT, 20, "夜晚选择一名好人击杀")

    def get_valid_targets(self, player: "Player", game: "Game") -> List[int]:
        return [p.seat for p in game.get_alive_players() if p.role.team != "werewolf"]

    def execute(self, player: "Player", target_seat: Optional[int], game: "Game") -> SkillResult:
        target = game.get_player_by_seat(target_seat)
        if not target:
            return SkillResult(False, "目标不存在")
        game.night_state.kill_target = target_seat
        return SkillResult(
            True,
            f"狼人选择击杀 {target_seat} 号 {target.nickname}",
            affected_seats=[target_seat],
            private=False,
        )


class GuardProtect(Skill):
    """守卫守护"""

    def __init__(self):
        super().__init__(
            "guard_protect", "守护", SkillPhase.NIGHT, 10, "夜晚守护一名玩家，不能连续守护同一人"
        )
        self._last_protected: Optional[int] = None

    def get_valid_targets(self, player: "Player", game: "Game") -> List[int]:
        targets = [p.seat for p in game.get_alive_players()]
        if self._last_protected in targets:
            targets.remove(self._last_protected)
        return targets

    def execute(self, player: "Player", target_seat: Optional[int], game: "Game") -> SkillResult:
        target = game.get_player_by_seat(target_seat)
        if not target:
            return SkillResult(False, "目标不存在")
        self._last_protected = target_seat
        game.night_state.protected = target_seat
        return SkillResult(True, f"守卫守护了 {target_seat} 号 {target.nickname}")


class WitchSave(Skill):
    """女巫解药"""

    def __init__(self):
        super().__init__(
            "witch_save", "使用解药", SkillPhase.NIGHT, 30, "救活今晚被杀的玩家（全局限一次）"
        )
        self._used = False

    def can_use(self, player: "Player", game: "Game") -> bool:
        return player.is_alive and not self._used and game.night_state.kill_target is not None

    def get_valid_targets(self, player: "Player", game: "Game") -> List[int]:
        if game.night_state.kill_target is not None:
            return [game.night_state.kill_target]
        return []

    def execute(self, player: "Player", target_seat: Optional[int], game: "Game") -> SkillResult:
        if self._used:
            return SkillResult(False, "解药已用完")
        target = game.get_player_by_seat(target_seat)
        if not target:
            return SkillResult(False, "目标不存在")
        self._used = True
        game.night_state.saved = target_seat
        return SkillResult(True, f"女巫使用解药救了 {target_seat} 号 {target.nickname}")


class WitchPoison(Skill):
    """女巫毒药"""

    def __init__(self):
        super().__init__(
            "witch_poison", "使用毒药", SkillPhase.NIGHT, 31, "毒死一名玩家（全局限一次）"
        )
        self._used = False

    def can_use(self, player: "Player", game: "Game") -> bool:
        return player.is_alive and not self._used

    def execute(self, player: "Player", target_seat: Optional[int], game: "Game") -> SkillResult:
        if self._used:
            return SkillResult(False, "毒药已用完")
        target = game.get_player_by_seat(target_seat)
        if not target:
            return SkillResult(False, "目标不存在")
        self._used = True
        game.night_state.poison_target = target_seat
        return SkillResult(True, f"女巫毒死了 {target_seat} 号 {target.nickname}")


class SeerCheck(Skill):
    """预言家查验"""

    def __init__(self):
        super().__init__("seer_check", "查验", SkillPhase.NIGHT, 40, "夜晚查验一名玩家的阵营")

    def execute(self, player: "Player", target_seat: Optional[int], game: "Game") -> SkillResult:
        target = game.get_player_by_seat(target_seat)
        if not target:
            return SkillResult(False, "目标不存在")
        camp = "狼人" if target.role.team == "werewolf" else "好人"
        return SkillResult(True, f"{target_seat} 号 {target.nickname} 是【{camp}】")


class HunterShoot(Skill):
    """猎人开枪"""

    def __init__(self):
        super().__init__("hunter_shoot", "开枪", SkillPhase.ON_DEATH, 0, "被淘汰时可以带走一名玩家")
        self._shot = False

    def can_use(self, player: "Player", game: "Game") -> bool:
        return not self._shot

    def execute(self, player: "Player", target_seat: Optional[int], game: "Game") -> SkillResult:
        if self._shot:
            return SkillResult(False, "已经开过枪了")
        target = game.get_player_by_seat(target_seat)
        if not target:
            return SkillResult(False, "目标不存在")
        self._shot = True
        return SkillResult(
            True,
            f"猎人开枪带走了 {target_seat} 号 {target.nickname}！",
            affected_seats=[target_seat],
            private=False,
        )


class IdiotReveal(Skill):
    """白痴翻牌"""

    def __init__(self):
        super().__init__(
            "idiot_reveal", "翻牌", SkillPhase.ON_DEATH, 0, "被投票出局时翻牌，失去投票权但保留座位"
        )
        self._revealed = False

    def can_use(self, player: "Player", game: "Game") -> bool:
        return not self._revealed

    def requires_target(self) -> bool:
        return False

    def can_skip(self) -> bool:
        return False

    def execute(self, player: "Player", target_seat: Optional[int], game: "Game") -> SkillResult:
        self._revealed = True
        player.can_vote = False
        player.is_alive = True  # 覆盖淘汰，保留座位
        return SkillResult(
            True,
            f"白痴翻牌！{player.seat} 号 {player.nickname} 保留座位，失去投票权",
            private=False,
        )
