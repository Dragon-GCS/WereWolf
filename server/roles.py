"""角色模块：定义所有角色基类和具体角色实现（含行动逻辑）"""

import copy
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
    ignore_guard: bool = False  # 机械狼破盾刀，无视守卫保护


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
    result_type: str = ""  # "seer" / "mirror" / "gargoyle" / "kill" / ""
    _extra: dict = field(default_factory=dict)  # 附加私有数据（不广播）


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
    join_wolf_kill: bool = False  # 是否参与夜晚狼人团队击杀投票

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

    def inspect_camp(self) -> str:
        """被查验时展示的阵营（好人/狼人）。子类可重写以实现伪装。"""
        return "狼人" if self.team == "狼人" else "好人"

    def inspect_role(self) -> str:
        """被查验时展示的角色名。子类可重写以实现伪装。"""
        return self.display_name

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
    join_wolf_kill = True

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

        # 吸血鬼转化玩家：第二轮起显示为狼人（覆盖角色本身的伪装）
        if target.vampire_converted and ctx.round >= 2:
            camp = "狼人"
        else:
            camp = target.role.inspect_camp()
        return ActionResult(
            True,
            f"{target_seat} 号 {target.nickname} 是【{camp}】",
            result_type="seer",
            _extra={"camp": camp},
        )


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


# ──────────────────────────── 新增角色 ────────────────────────────


class MirrorGirlRole(Role, role_name="灵镜少女"):
    """加强版预言家：每晚可查验一名玩家的具体身份（职业名）"""

    team = "神职"

    def get_valid_targets(self, player: "Player", ctx: RoleContext) -> list[int]:
        return [p.seat for p in ctx.get_alive_players() if p.seat != player.seat]

    def execute(
        self, player: "Player", target_seat: Optional[int], ctx: RoleContext
    ) -> ActionResult:
        if not target_seat:
            raise ValueError("灵镜少女查验必须指定目标")
        target = ctx.get_player_by_seat(target_seat)
        if not target.role:
            raise ValueError(f"目标 {target_seat} 没有角色信息")

        role_display = target.role.inspect_role()
        camp = target.role.inspect_camp()

        # 吸血鬼转化额外信息
        extra_info: Optional[str] = None
        if target.vampire_converted:
            extra_info = f"（原始身份：{target.role.display_name}，已被吸血鬼转化）"

        return ActionResult(
            True,
            f"{target_seat} 号 {target.nickname} 是【{role_display}】",
            result_type="mirror",
            _extra={"role_display": role_display, "extra_info": extra_info, "camp": camp},
        )


class BlackWolfKingRole(Role, role_name="黑狼王"):
    """狼人版猎人：死亡时可以开枪带走一名玩家"""

    team = "狼人"
    join_wolf_kill = True

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
            return ActionResult(True, "黑狼王选择放弃开枪", private=False)
        target = ctx.get_player_by_seat(target_seat)
        self._shot = True
        return ActionResult(
            True,
            f"黑狼王开枪带走了 {target_seat} 号 {target.nickname}！",
            affected_seats=[target_seat],
            private=False,
        )


class VampireRole(Role, role_name="吸血鬼"):
    """第一晚可指定一名玩家将其阵营转变为狼人（身份和技能不变，仅阵营变化）"""

    team = "狼人"
    join_wolf_kill = True

    def __init__(self, config: RoleConfig, raw: dict):
        super().__init__(config, raw)
        self._used = False

    def can_use(self, player: "Player", ctx: RoleContext) -> bool:
        return player.is_alive and not self._used and ctx.round == 1

    def get_valid_targets(self, player: "Player", ctx: RoleContext) -> list[int]:
        return [p.seat for p in ctx.get_alive_players() if p.seat != player.seat]

    def execute(
        self, player: "Player", target_seat: Optional[int], ctx: RoleContext
    ) -> ActionResult:
        if target_seat is None:
            return ActionResult(True, "吸血鬼放弃转化", private=False)
        target = ctx.get_player_by_seat(target_seat)
        if not target.role:
            return ActionResult(False, "目标没有角色信息")
        self._used = True
        target.vampire_converted = True
        target.vampire_conversion_round = ctx.round
        target.team_override = "狼人"
        return ActionResult(
            True,
            f"吸血鬼将 {target_seat} 号 {target.nickname} 转化为狼人阵营",
            affected_seats=[target_seat],
            result_type="vampire_convert",
            private=False,
        )


class GravediggerRole(Role, role_name="守墓人"):
    """神职，可以获得白天出局玩家的身份（被动，游戏逻辑在 game.py 中处理）"""

    team = "神职"

    def get_valid_targets(self, player: "Player", ctx: RoleContext) -> list[int]:
        return []

    def execute(
        self, player: "Player", target_seat: Optional[int], ctx: RoleContext
    ) -> ActionResult:
        return ActionResult(True, "守墓人待机")


class GargoyleRole(Role, role_name="石像鬼"):
    """狼人版灵镜少女：每晚可查验一名玩家的具体身份。所有其他狼人出局后才能刀人"""

    team = "狼人"

    def get_valid_targets(self, player: "Player", ctx: RoleContext) -> list[int]:
        # 判断是否有其他活着的狼人（不含自身）
        other_wolves = [
            p for p in ctx.get_alive_players()
            if p.seat != player.seat and p.team == "狼人"
        ]
        if other_wolves:
            # 查验模式：不能选自己
            return [p.seat for p in ctx.get_alive_players() if p.seat != player.seat]
        else:
            # 刀人模式：可以选所有活玩家（包括技术上选自己也无意义，排除）
            return [p.seat for p in ctx.get_alive_players() if p.seat != player.seat]

    def execute(
        self, player: "Player", target_seat: Optional[int], ctx: RoleContext
    ) -> ActionResult:
        if target_seat is None:
            return ActionResult(True, "石像鬼放弃行动", private=False)

        target = ctx.get_player_by_seat(target_seat)
        if not target.role:
            return ActionResult(False, "目标没有角色信息")

        # 始终做查验；末狼时的刀人由 _get_role_players 将石像鬼加入狼人击杀轮处理
        role_display = target.role.inspect_role()
        camp = target.role.inspect_camp()
        return ActionResult(
            True,
            f"{target_seat} 号 {target.nickname} 是【{role_display}】",
            result_type="gargoyle",
            _extra={"role_display": role_display, "camp": camp},
        )


class KnightRole(Role, role_name="骑士"):
    """拥有白天决斗能力的神职角色（每局只能决斗一次）"""

    team = "神职"

    def __init__(self, config: RoleConfig, raw: dict):
        super().__init__(config, raw)
        self._dueled = False

    def can_use(self, player: "Player", ctx: RoleContext) -> bool:
        return player.is_alive and not self._dueled

    def get_valid_targets(self, player: "Player", ctx: RoleContext) -> list[int]:
        return [p.seat for p in ctx.get_alive_players() if p.seat != player.seat]

    def execute(
        self, player: "Player", target_seat: Optional[int], ctx: RoleContext
    ) -> ActionResult:
        if self._dueled:
            return ActionResult(False, "已经决斗过了")
        if target_seat is None:
            return ActionResult(False, "骑士决斗必须指定目标")
        target = ctx.get_player_by_seat(target_seat)
        if not target.role:
            return ActionResult(False, "目标没有角色信息")
        self._dueled = True

        if target.team == "狼人":
            # 决斗对象是狼人 → 狼人死亡，进入黑夜
            return ActionResult(
                True,
                f"骑士决斗 {target_seat} 号 {target.nickname}！目标是狼人，即刻死亡！",
                affected_seats=[target_seat],
                result_type="knight_win",
                private=False,
            )
        else:
            # 决斗对象是好人 → 骑士死亡
            return ActionResult(
                True,
                f"骑士决斗 {target_seat} 号 {target.nickname}！目标是好人，骑士身亡！",
                affected_seats=[player.seat],
                result_type="knight_lose",
                private=False,
            )

    def to_dict(self) -> "RoleDict":
        d = super().to_dict()
        d["dueled"] = self._dueled
        return d


class MechWolfRole(Role, role_name="机械狼"):
    """第一晚学习指定玩家技能的狼人。被查验时显示为学习目标的身份。
    其他狼人全部死亡后才能在夜晚刀人（可无视守卫）。不与其他狼人互相知晓。"""

    team = "狼人"

    def __init__(self, config: RoleConfig, raw: dict):
        super().__init__(config, raw)
        self.learned_role: Optional["Role"] = None  # 学到的技能对应角色实例
        self.learned_display: str = ""              # 伪装时展示的角色名
        self.learned_team: str = ""                 # 学习目标的原始阵营
        self.ignore_guard: bool = False             # 是否获得了破盾刀（学习了狼人）
        self._learned: bool = False                 # 是否已完成第一晚学习

    def can_use(self, player: "Player", ctx: RoleContext) -> bool:
        if not player.is_alive:
            return False
        if not self._learned:
            # 第一晚：学习技能
            return ctx.round == 1
        # 已学习：检查行动模式
        other_wolves = [
            p for p in ctx.get_alive_players()
            if p.seat != player.seat and p.team == "狼人"
        ]
        # 有无其他狼均委托给 learned_role；破盾刀（learned_role=None）时末狼由狼人击杀轮处理
        if self.learned_role is not None:
            return self.learned_role.can_use(player, ctx)
        return False

    def get_valid_targets(self, player: "Player", ctx: RoleContext) -> list[int]:
        if not self._learned:
            return [p.seat for p in ctx.get_alive_players() if p.seat != player.seat]

        other_wolves = [
            p for p in ctx.get_alive_players()
            if p.seat != player.seat and p.team == "狼人"
        ]
        if self.learned_role is not None:
            if isinstance(self.learned_role, WitchRole):
                return self.learned_role.get_poison_targets(player, ctx)
            return self.learned_role.get_valid_targets(player, ctx)
        return []

    def execute(
        self, player: "Player", target_seat: Optional[int], ctx: RoleContext
    ) -> ActionResult:
        if not self._learned:
            # 第一晚学习模式
            if target_seat is None:
                return ActionResult(False, "机械狼必须指定学习目标")
            target = ctx.get_player_by_seat(target_seat)
            if not target.role:
                return ActionResult(False, "目标没有角色信息")
            self._learned = True
            original_team = target.role.team
            self.learned_team = original_team
            self.learned_display = target.role.display_name

            # 通知游戏层（game.py）更新机械狼前端显示
            # to_dict 已包含 learned_display，_broadcast_your_info 会自动推送

            if isinstance(target.role, WitchRole):
                # 女巫：只继承毒药能力（深拷贝，避免共享状态）
                self.learned_role = copy.deepcopy(target.role)
                return ActionResult(
                    True,
                    f"机械狼从 {target_seat} 号 {target.nickname} 学习了【{self.learned_display}·毒药】",
                )
            elif target.role.team == "狼人":
                # 狼人：获得破盾刀
                self.ignore_guard = True
                self.learned_role = None  # 无需委托，直接刀人（走无他狼逻辑）
                return ActionResult(
                    True,
                    f"机械狼从 {target_seat} 号 {target.nickname} 学习了【破盾刀】，可无视守卫保护",
                )
            else:
                # 普通角色：深拷贝，避免与原玩家共享内部状态（如守卫的 _last_protected）
                self.learned_role = copy.deepcopy(target.role)
                return ActionResult(
                    True,
                    f"机械狼从 {target_seat} 号 {target.nickname} 学习了【{self.learned_display}】技能",
                )

        # 已学习后：始终委托给学到的技能（无论是否末狼）
        # 末狼时的刀人由 _get_role_players 将机械狼加入狼人击杀轮处理
        if self.learned_role is None:
            return ActionResult(True, "机械狼待机（无可用技能）")
        if isinstance(self.learned_role, WitchRole):
            if target_seat is None:
                return ActionResult(True, "机械狼放弃使用毒药")
            return self.learned_role.execute_poison(player, target_seat, ctx)
        return self.learned_role.execute(player, target_seat, ctx)

    def inspect_camp(self) -> str:
        apparent_team = self.learned_team if self.learned_team else self.team
        return "狼人" if apparent_team == "狼人" else "好人"

    def inspect_role(self) -> str:
        return self.learned_display if self.learned_display else self.display_name

    def to_dict(self) -> "RoleDict":
        d = super().to_dict()
        if self.learned_display:
            d["learned_display"] = self.learned_display
        return d


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
