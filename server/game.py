"""游戏核心：状态机与游戏流程"""

import asyncio
import logging
import random
from collections import Counter
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, Dict, List, Optional, Tuple

# 夜晚技能的开眼/闭眼公告 (open_msg, close_msg, open_audio, close_audio)
SKILL_NIGHT_ANNOUNCE: Dict[str, Tuple[str, str, str, str]] = {
    "guard_protect": (
        "守卫请睁眼，守护一名玩家",
        "守卫请闭眼",
        "guard_open.mp3",
        "guard_close.mp3",
    ),
    "werewolf_kill": (
        "狼人请睁眼，选择今晚击杀的目标",
        "狼人请闭眼",
        "werewolf_open.mp3",
        "werewolf_close.mp3",
    ),
    "witch_save": ("女巫请睁眼", None, "witch_open.mp3", None),
    "witch_poison": (None, "女巫请闭眼", None, "witch_close.mp3"),
    "seer_check": (
        "预言家请睁眼，查验一名玩家的身份",
        "预言家请闭眼",
        "seer_open.mp3",
        "seer_close.mp3",
    ),
}

from .events import EventLog
from .player import Player
from .roles import create_role
from .skills import Skill, SkillPhase, SkillResult

if TYPE_CHECKING:
    from .connection_manager import ConnectionManager

logger = logging.getLogger(__name__)


class GamePhase(str, Enum):
    WAITING = "waiting"
    NIGHT = "night"
    DAWN = "dawn"
    DAY_DISCUSS = "day_discuss"
    DAY_VOTE = "day_vote"
    GAME_OVER = "game_over"


PHASE_DISPLAY = {
    GamePhase.WAITING: "等待开始",
    GamePhase.NIGHT: "黑夜",
    GamePhase.DAWN: "黎明",
    GamePhase.DAY_DISCUSS: "白天·讨论",
    GamePhase.DAY_VOTE: "白天·投票",
    GamePhase.GAME_OVER: "游戏结束",
}


@dataclass
class NightState:
    kill_target: Optional[int] = None
    saved: Optional[int] = None
    poison_target: Optional[int] = None
    protected: Optional[int] = None


class Game:
    """游戏主控类（单例）"""

    _instance: Optional["Game"] = None

    def __init__(self, cm: "ConnectionManager"):
        self.cm = cm
        self.players: List[Player] = []
        self.phase: GamePhase = GamePhase.WAITING
        self.round: int = 0
        self.night_state: NightState = NightState()
        self.events: EventLog = EventLog()
        self.winner: Optional[str] = None
        self.current_speaker_seat: Optional[int] = None
        self.voting_candidates: List[int] = []
        self.current_action: str = ""  # 当前详细阶段描述，广播给所有客户端

        # 动作协调
        self._pending_future: Optional[asyncio.Future] = None
        self._pending_seats: set = set()
        self._pending_votes: Dict[int, Optional[int]] = {}

        # 游戏任务
        self._game_task: Optional[asyncio.Task] = None

    # ──────────────────────────── 玩家管理 ────────────────────────────

    def add_or_update_player(self, seat: int, nickname: str) -> Tuple[bool, str, bool]:
        """玩家加入/更新信息。返回 (success, message, is_reconnect)

        若座位已有玩家则视为重连（更新昵称，保留游戏状态）。
        """
        existing = self.get_player_by_seat(seat)
        if existing:
            existing.nickname = nickname
            is_reconnect = True
        else:
            player = Player(seat, nickname)
            self.players.append(player)
            is_reconnect = False

        action = "重连" if is_reconnect else "加入"
        self.events.log(
            "player_join", f"{seat} 号 {nickname} {action}", {"seat": seat, "nickname": nickname}
        )
        return True, f"{action}成功", is_reconnect

    def get_player_by_seat(self, seat: Optional[int]) -> Optional[Player]:
        if seat is None:
            return None
        for p in self.players:
            if p.seat == seat:
                return p
        return None

    def get_alive_players(self) -> List[Player]:
        return [p for p in self.players if p.is_alive]

    # ──────────────────────────── 游戏流程 ────────────────────────────

    async def start_game(self, role_list: List[str]) -> Tuple[bool, str]:
        """分配角色并启动游戏循环"""
        if self.phase != GamePhase.WAITING:
            return False, "游戏已在进行中"
        if len(self.players) != len(role_list):
            return False, f"玩家数量 ({len(self.players)}) 与角色数量 ({len(role_list)}) 不符"

        shuffled_roles = role_list.copy()
        random.shuffle(shuffled_roles)
        sorted_players = sorted(self.players, key=lambda p: p.seat)

        for player, role_name in zip(sorted_players, shuffled_roles):
            player.role = create_role(role_name)
            player.is_alive = True
            player.can_vote = True
            player.is_sheriff = False

        self.events.log("game_start", f"游戏开始，共 {len(self.players)} 名玩家")

        # 发送私密角色信息
        for player in self.players:
            await self.cm.send_to_seat(
                player.seat,
                {
                    "type": "your_info",
                    "data": player.to_private_dict(),
                },
            )

        self._game_task = asyncio.create_task(self._game_loop())
        return True, "游戏开始"

    async def _game_loop(self):
        """主游戏循环"""
        try:
            while True:
                self.round += 1
                self.current_action = f"第 {self.round} 轮"
                logger.info("===== 第 %d 轮 开始 =====", self.round)
                await self._run_night()

                # 黎明阶段
                deaths = await self._run_dawn()
                await self._broadcast_game_state()

                winner = self._check_win()
                if winner:
                    await self._end_game(winner)
                    return

                # 白天阶段
                eliminated = await self._run_day(deaths)

                if eliminated is not None:
                    await self._handle_on_death(eliminated, cause="vote")

                await self._broadcast_game_state()

                winner = self._check_win()
                if winner:
                    await self._end_game(winner)
                    return

        except asyncio.CancelledError:
            logger.info("游戏循环已取消")
        except Exception:
            logger.exception("游戏循环异常")

    # ──────────────────────────── 夜晚 ────────────────────────────

    async def _run_night(self):
        self.phase = GamePhase.NIGHT
        self.night_state = NightState()
        self.current_speaker_seat = None
        self.current_action = f"第 {self.round} 夜 · 天黑请闭眼"
        self.events.log("phase_change", f"第 {self.round} 夜开始")
        await self._broadcast_notification(
            f"第 {self.round} 夜降临，天黑请闭眼...", audio="night_start.mp3"
        )
        await self._broadcast_game_state()

        for group in self._get_night_action_groups():
            skill = group[0][1]
            able_players = [p for p, s in group if p.is_alive and skill.can_use(p, self)]
            announce = SKILL_NIGHT_ANNOUNCE.get(skill.name)

            # 即使没有玩家能用该技能，有些技能仍需广播「请睁眼/请闭眼」
            if announce:
                open_msg, close_msg, open_audio, close_audio = announce
                if open_msg:
                    self.current_action = open_msg
                    self.events.log("night_action", open_msg)
                    await self._broadcast_notification(open_msg, audio=open_audio)
                    await self._broadcast_game_state()

            if not able_players:
                if announce and announce[1]:  # 有 close_msg
                    self.current_action = announce[1]
                    self.events.log("night_action", announce[1])
                    await self._broadcast_notification(announce[1], audio=announce[3])
                    await self._broadcast_game_state()
                continue

            seats = [p.seat for p in able_players]
            results = await self._request_action(
                seats,
                skill,
                message=skill.display_name,
                timeout=120.0,
            )

            if skill.name == "werewolf_kill":
                # 狼人团队：取多数票
                votes = [v for v in results.values() if v is not None]
                if votes:
                    target = Counter(votes).most_common(1)[0][0]
                    r = skill.execute(able_players[0], target, self)
                    self.events.log(
                        "skill_used", r.message, {"skill": skill.name, "target": target}
                    )
            else:
                for seat, target in results.items():
                    player = self.get_player_by_seat(seat)
                    if player:
                        if target is not None or not skill.requires_target():
                            result = skill.execute(player, target, self)
                            self.events.log(
                                "skill_used",
                                result.message,
                                {"skill": skill.name, "seat": seat, "target": target},
                            )
                            # 仅将结果发给操作者
                            await self.cm.send_to_seat(
                                seat,
                                {
                                    "type": "action_result",
                                    "data": {"message": result.message, "success": result.success},
                                },
                            )

            # 闭眼公告
            if announce and announce[1]:
                self.current_action = announce[1]
                self.events.log("night_action", announce[1])
                await self._broadcast_notification(announce[1], audio=announce[3])
                await self._broadcast_game_state()

    def _get_night_action_groups(self) -> List[List[Tuple[Player, Skill]]]:
        """按优先级分组，返回 [(player, skill), ...] 的列表"""
        priority_map: Dict[Tuple[int, str], List[Tuple[Player, Skill]]] = {}
        for player in self.get_alive_players():
            for skill in player.role.skills:
                if skill.phase == SkillPhase.NIGHT:
                    key = (skill.priority, skill.name)
                    priority_map.setdefault(key, []).append((player, skill))
        return [priority_map[k] for k in sorted(priority_map.keys())]

    # ──────────────────────────── 黎明 ────────────────────────────

    async def _run_dawn(self) -> List[int]:
        self.phase = GamePhase.DAWN
        self.current_action = f"第 {self.round} 夜 · 黎明公告"
        self.events.log("phase_change", f"第 {self.round} 夜 黎明")
        deaths: List[int] = []

        kill = self.night_state.kill_target
        if kill is not None:
            if kill == self.night_state.saved:
                self.events.log("night_save", f"{kill} 号被解药救活")
            elif kill == self.night_state.protected:
                self.events.log("night_protect", f"{kill} 号被守卫保护")
            else:
                deaths.append(kill)

        poison = self.night_state.poison_target
        if poison is not None and poison not in deaths:
            deaths.append(poison)

        for seat in deaths:
            player = self.get_player_by_seat(seat)
            if player and player.is_alive:
                player.is_alive = False
                self.events.log(
                    "player_died", f"{seat} 号 {player.nickname} 夜晚死亡", {"seat": seat}
                )

        if deaths:
            names = "、".join(
                f"{s} 号 {self.get_player_by_seat(s).nickname}"
                for s in deaths
                if self.get_player_by_seat(s)
            )
            await self._broadcast_notification(f"昨夜，{names} 死亡", audio="dawn_death.mp3")
        else:
            await self._broadcast_notification("昨夜平安，没有玩家死亡", audio="dawn_peace.mp3")

        return deaths

    # ──────────────────────────── 白天 ────────────────────────────

    async def _run_day(self, deaths: List[int]) -> Optional[int]:
        self.phase = GamePhase.DAY_DISCUSS
        self.current_action = f"第 {self.round} 天 · 请依次发言"
        self.events.log("phase_change", f"第 {self.round} 天白天")
        await self._broadcast_notification(
            f"第 {self.round} 天，天亮了，请依次发言", audio="day_start.mp3"
        )
        await self._broadcast_game_state()

        # 发言顺序：从死亡玩家下一个座位开始，按座位顺序
        alive = sorted(self.get_alive_players(), key=lambda p: p.seat)
        if deaths and alive:
            start = min(deaths) + 1
            reordered = sorted(alive, key=lambda p: (p.seat - start) % 100)
        else:
            reordered = alive

        for player in reordered:
            self.current_speaker_seat = player.seat
            self.current_action = f"{player.seat} 号 {player.nickname} 发言中"
            await self._broadcast_game_state()
            self.events.log(
                "speech_start",
                f"{player.seat} 号 {player.nickname} 开始发言",
                {"seat": player.seat},
            )
            await self._wait_speech_end(player.seat, timeout=180)

        self.current_speaker_seat = None

        # 投票阶段
        self.phase = GamePhase.DAY_VOTE
        self.current_action = "投票阶段 · 请选择淘汰玩家"
        await self._broadcast_notification(
            "请投票选择淘汰玩家（0 表示弃票）", audio="vote_start.mp3"
        )
        await self._broadcast_game_state()

        eliminated = await self._run_vote()
        return eliminated

    async def _run_vote(self) -> Optional[int]:
        """发起投票，返回被淘汰的座位号"""
        eligible_voters = [p for p in self.get_alive_players() if p.can_vote]
        candidates = [p.seat for p in self.get_alive_players()]
        self.voting_candidates = candidates

        seats = [p.seat for p in eligible_voters]
        votes = await self._request_vote(seats, candidates, timeout=120.0)

        # 统计票数
        tally: Counter = Counter()
        for voter_seat, target_seat in votes.items():
            if target_seat in candidates:
                weight = (
                    1.5
                    if self.get_player_by_seat(voter_seat)
                    and self.get_player_by_seat(voter_seat).is_sheriff
                    else 1
                )
                tally[target_seat] += weight

        self.voting_candidates = []

        if not tally:
            await self._broadcast_notification("本轮无人被淘汰（无有效票）")
            return None

        max_votes = max(tally.values())
        top = [s for s, v in tally.items() if v == max_votes]

        if len(top) > 1:
            await self._broadcast_notification(
                f"平票！{' 和 '.join(str(s) + '号' for s in top)} 均无效，本轮无人出局"
            )
            self.events.log("vote_tie", f"平票：{top}")
            return None

        loser_seat = top[0]
        loser = self.get_player_by_seat(loser_seat)
        if loser:
            loser.is_alive = False
            self.events.log(
                "player_eliminated",
                f"{loser_seat} 号 {loser.nickname} 被投票出局",
                {"seat": loser_seat},
            )
            await self._broadcast_notification(
                f"{loser_seat} 号 {loser.nickname} 被淘汰！", audio="eliminated.mp3"
            )
        return loser_seat

    # ──────────────────────────── 死亡技能 ────────────────────────────

    async def _handle_on_death(self, seat: int, cause: str = "vote"):
        """处理死亡触发的技能（猎人开枪、白痴翻牌）"""
        player = self.get_player_by_seat(seat)
        if not player:
            return

        for skill in player.role.skills:
            if skill.phase == SkillPhase.ON_DEATH and skill.can_use(player, self):
                if not skill.requires_target():
                    result = skill.execute(player, None, self)
                    await self._broadcast_notification(result.message)
                    if not result.success:
                        continue
                    # 白痴翻牌：复活了
                    await self._broadcast_game_state()
                    return

                # 需要选目标（猎人）
                await self._broadcast_notification(
                    f"{seat} 号 {player.nickname} 触发技能：{skill.display_name}",
                    audio="hunter_shoot.mp3",
                )
                results = await self._request_action(
                    [seat],
                    skill,
                    message=f"请选择开枪目标",
                    timeout=60.0,
                )
                target = results.get(seat)
                if target is not None:
                    result = skill.execute(player, target, self)
                    await self._broadcast_notification(result.message)
                    if result.affected_seats:
                        for s in result.affected_seats:
                            killed = self.get_player_by_seat(s)
                            if killed:
                                killed.is_alive = False
                                self.events.log(
                                    "player_died",
                                    f"{s} 号 {killed.nickname} 被猎人射杀",
                                    {"seat": s},
                                )

    # ──────────────────────────── 胜负判断 ────────────────────────────

    def _check_win(self) -> Optional[str]:
        alive = self.get_alive_players()
        wolves = [p for p in alive if p.role.team == "werewolf"]
        villagers = [p for p in alive if p.role.team != "werewolf"]

        if not wolves:
            return "villager"
        if len(wolves) >= len(villagers):
            return "werewolf"
        return None

    async def _end_game(self, winner: str):
        self.phase = GamePhase.GAME_OVER
        self.winner = winner
        label = "狼人" if winner == "werewolf" else "好人"
        self.events.log("game_over", f"游戏结束，{label}阵营胜利")
        await self._broadcast_notification(f"游戏结束！{label}阵营胜利！", audio="game_over.mp3")
        # 揭示所有角色
        await self._broadcast_game_state(reveal_roles=True)

    # ──────────────────────────── 动作收集 ────────────────────────────

    async def _request_action(
        self,
        seats: List[int],
        skill: Skill,
        message: str = "",
        timeout: float = 120.0,
    ) -> Dict[int, Optional[int]]:
        """向指定座位请求技能操作，返回 {seat: target}"""
        sample_player = self.get_player_by_seat(seats[0])
        valid_targets = skill.get_valid_targets(sample_player, self) if sample_player else []

        self._pending_future = asyncio.get_event_loop().create_future()
        self._pending_seats = set(seats)
        self._pending_votes = {}

        for seat in seats:
            await self.cm.send_to_seat(
                seat,
                {
                    "type": "action_request",
                    "data": {
                        "skill": skill.name,
                        "skill_display": skill.display_name,
                        "message": message or f"请使用技能：{skill.display_name}",
                        "valid_targets": valid_targets,
                        "requires_target": skill.requires_target(),
                        "can_skip": skill.can_skip(),
                        "is_group": len(seats) > 1,
                    },
                },
            )

        try:
            await asyncio.wait_for(self._pending_future, timeout=timeout)
        except asyncio.TimeoutError:
            logger.warning("动作请求超时，座位：%s", seats)

        result = dict(self._pending_votes)
        self._pending_future = None
        self._pending_seats = set()
        self._pending_votes = {}

        # 清除客户端的动作面板
        for seat in seats:
            await self.cm.send_to_seat(seat, {"type": "action_clear"})

        return result

    async def _request_vote(
        self,
        voter_seats: List[int],
        candidates: List[int],
        timeout: float = 120.0,
    ) -> Dict[int, Optional[int]]:
        """向指定座位收集投票，返回 {voter_seat: target_seat}"""
        self._pending_future = asyncio.get_event_loop().create_future()
        self._pending_seats = set(voter_seats)
        self._pending_votes = {}

        for seat in voter_seats:
            await self.cm.send_to_seat(
                seat,
                {
                    "type": "vote_request",
                    "data": {
                        "message": "请投票（选择要淘汰的玩家，0 表示弃票）",
                        "candidates": candidates,
                    },
                },
            )

        try:
            await asyncio.wait_for(self._pending_future, timeout=timeout)
        except asyncio.TimeoutError:
            logger.warning(
                "投票超时，未投票座位：%s", self._pending_seats - set(self._pending_votes.keys())
            )

        result = dict(self._pending_votes)
        self._pending_future = None
        self._pending_seats = set()
        self._pending_votes = {}

        for seat in voter_seats:
            await self.cm.send_to_seat(seat, {"type": "action_clear"})

        return result

    async def _wait_speech_end(self, seat: int, timeout: float = 180.0):
        """等待玩家发言结束信号"""
        self._pending_future = asyncio.get_event_loop().create_future()
        self._pending_seats = {seat}
        self._pending_votes = {}

        await self.cm.send_to_seat(
            seat,
            {
                "type": "speech_turn",
                "data": {"message": "请发言，发言结束后点击「发言结束」"},
            },
        )

        try:
            await asyncio.wait_for(self._pending_future, timeout=timeout)
        except asyncio.TimeoutError:
            pass

        self._pending_future = None
        self._pending_seats = set()
        self._pending_votes = {}

    # ──────────────────────────── 外部提交接口 ────────────────────────────

    def submit_action(self, seat: int, target: Optional[int]) -> bool:
        """WebSocket 处理器调用此方法提交玩家动作"""
        if seat not in self._pending_seats:
            return False

        self._pending_votes[seat] = target
        self._check_pending_complete()
        return True

    def submit_speech_end(self, seat: int) -> bool:
        """玩家发言结束"""
        if seat not in self._pending_seats:
            return False
        self._pending_votes[seat] = None
        self._check_pending_complete()
        return True

    def _check_pending_complete(self):
        """检查所有待收集动作是否全部到位"""
        if (
            self._pending_future
            and not self._pending_future.done()
            and self._pending_seats.issubset(set(self._pending_votes.keys()))
        ):
            self._pending_future.set_result(self._pending_votes)

    # ──────────────────────────── 广播辅助 ────────────────────────────

    async def _broadcast_notification(self, message: str, audio: Optional[str] = None):
        self.events.log("notification", message)
        await self.cm.broadcast({"type": "notification", "data": {"message": message}})
        if audio:
            await self.cm.broadcast_audio(audio)

    async def _broadcast_game_state(self, reveal_roles: bool = False):
        """广播公开游戏状态"""
        state = self._build_public_state(reveal_roles)
        await self.cm.broadcast({"type": "game_state", "data": state})

    def _build_public_state(self, reveal_roles: bool = False) -> dict:
        return {
            "phase": self.phase.value,
            "phase_display": PHASE_DISPLAY.get(self.phase, self.phase.value),
            "current_action": self.current_action,
            "round": self.round,
            "winner": self.winner,
            "current_speaker_seat": self.current_speaker_seat,
            "voting_candidates": self.voting_candidates,
            "players": [
                {
                    **p.to_public_dict(),
                    "role_display": p.role.display_name
                    if (reveal_roles and p.role)
                    else (p.role.display_name if not p.is_alive and p.role else None),
                }
                for p in sorted(self.players, key=lambda x: x.seat)
            ],
        }

    def get_public_state(self) -> dict:
        return self._build_public_state()

    # ──────────────────────────── 管理员命令 ────────────────────────────

    async def admin_force_kill(self, seat: int) -> str:
        player = self.get_player_by_seat(seat)
        if not player:
            return "玩家不存在"
        player.is_alive = False
        self.events.log("admin_kill", f"管理员强制杀死 {seat} 号 {player.nickname}", {"seat": seat})
        await self._broadcast_game_state()
        return f"{seat} 号 {player.nickname} 已标记为死亡"

    async def admin_force_revive(self, seat: int) -> str:
        player = self.get_player_by_seat(seat)
        if not player:
            return "玩家不存在"
        player.is_alive = True
        self.events.log("admin_revive", f"管理员复活 {seat} 号 {player.nickname}", {"seat": seat})
        await self._broadcast_game_state()
        return f"{seat} 号 {player.nickname} 已复活"

    async def admin_set_sheriff(self, seat: int) -> str:
        for p in self.players:
            p.is_sheriff = False
        player = self.get_player_by_seat(seat)
        if player:
            player.is_sheriff = True
            self.events.log("admin_sheriff", f"管理员设置 {seat} 号 {player.nickname} 为警长")
        await self._broadcast_game_state()
        return f"{seat} 号已被设为警长" if player else "玩家不存在"

    async def admin_skip_phase(self) -> str:
        """强制跳过当前等待（设置 future 结果）"""
        if self._pending_future and not self._pending_future.done():
            self._pending_future.set_result({})
            return "已跳过当前步骤"
        return "当前无等待步骤"

    async def admin_reset(self) -> str:
        """重置游戏，回到等待状态"""
        if self._game_task and not self._game_task.done():
            self._game_task.cancel()
        for p in self.players:
            p.is_alive = True
            p.can_vote = True
            p.is_sheriff = False
            p.role = None
        self.phase = GamePhase.WAITING
        self.round = 0
        self.winner = None
        self.night_state = NightState()
        self.voting_candidates = []
        self.current_speaker_seat = None
        self.current_action = ""
        self.events.log("admin_reset", "管理员重置游戏")
        await self._broadcast_game_state()
        return "游戏已重置"

    def admin_rollback(self, event_id: int) -> str:
        """日志回退（仅删除日志记录，不恢复游戏状态）"""
        removed = self.events.truncate_after(event_id)
        return f"已删除 {len(removed)} 条日志记录（游戏状态需手动修正）"


# 全局单例（由 app.py 初始化）
_game_instance: Optional[Game] = None


def get_game() -> Game:
    if _game_instance is None:
        raise RuntimeError("Game 未初始化")
    return _game_instance


def init_game(cm: "ConnectionManager") -> Game:
    global _game_instance
    _game_instance = Game(cm)
    return _game_instance
