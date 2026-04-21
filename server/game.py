"""游戏核心：状态机与游戏流程"""

import asyncio
import logging
import random
from collections import Counter, defaultdict
from contextlib import suppress
from enum import Enum
from typing import TYPE_CHECKING, ClassVar, Optional

from server.messages import GameStateData, SeerResultItem, SeerResultsData

from .events import EventLog
from .player import Player
from .roles import (
    NightState,
    Role,
    RoleContext,
    RolePhase,
    WitchRole,
    build_role_from_config,
    build_role_map,
)

if TYPE_CHECKING:
    from .connection_manager import ConnectionManager

logger = logging.getLogger(__name__)


class GamePhase(str, Enum):
    WAITING = "waiting"
    NIGHT = "night"
    SHERIFF = "sheriff"
    DAY_DISCUSS = "day_discuss"
    DAY_VOTE = "day_vote"
    GAME_OVER = "game_over"


PHASE_DISPLAY = {
    GamePhase.WAITING: "等待开始",
    GamePhase.NIGHT: "黑夜",
    GamePhase.SHERIFF: "竞选警长",
    GamePhase.DAY_DISCUSS: "白天·讨论",
    GamePhase.DAY_VOTE: "白天·投票",
    GamePhase.GAME_OVER: "游戏结束",
}


class Game:
    """游戏主控类（单例）"""

    _instance: Optional["Game"] = None

    def __init__(self, cm: "ConnectionManager"):
        self.cm = cm
        self.players: list[Player] = []
        self.phase: GamePhase = GamePhase.WAITING
        self.round: int = 0
        self.night_state: NightState = NightState()
        self.events: EventLog = EventLog()
        self.winner: Optional[str] = None
        self.current_speaker_seat: Optional[int] = None
        self.voting_candidates: list[int] = []
        self.current_action: str = ""

        # 从配置加载的数据
        self._role_map: dict[str, dict] = {}  # {role_name: raw_role_dict}
        self._night_actions: list[str] = []  # game_stages 中夜晚阶段的角色名列表
        self._preset_name: str = ""  # 当前使用的预设名称

        # 动作协调
        self._pending_future: Optional[asyncio.Future] = None
        self._pending_seats: set = set()
        self._pending_votes: dict[int, int | dict | None] = {}

        # 游戏任务
        self._game_task: Optional[asyncio.Task] = None

        # 预言家查验记录 {seer_seat: [SeerResult, ...]}
        self.seer_results: dict[int, list[SeerResultItem]] = {}

        # 上帝面板强制进入黑夜标志
        self._force_night: bool = False

    # ──────────────────────────── 配置加载 ────────────────────────────

    def load_config(self, config: dict) -> None:
        """从预设配置字典加载角色定义和流程配置"""
        self._role_map = build_role_map(config.get("roles", []))

        self._night_actions = []
        for stage in config.get("game_stages", []):
            if stage["name"] == "夜晚":
                # actions 现在是纯角色名字符串列表
                self._night_actions = [a for a in stage.get("actions", []) if isinstance(a, str)]

    # ──────────────────────────── 玩家管理 ────────────────────────────

    def add_or_update_player(self, seat: int, nickname: str) -> tuple[bool, str, bool]:
        try:
            player = self.get_player_by_seat(seat)
            player.nickname = nickname
            is_reconnect = True
        except KeyError:
            player = Player(seat, nickname)
            self.players.append(player)
            is_reconnect = False

        action = "重连" if is_reconnect else "加入"
        self.events.log(
            "player_join", f"{seat} 号 {nickname} {action}", {"seat": seat, "nickname": nickname}
        )
        return True, f"{action}成功", is_reconnect

    def get_player_by_seat(self, seat: int) -> Player:
        for p in self.players:
            if p.seat == seat:
                return p
        msg = f"未找到座位 {seat} 的玩家"
        raise KeyError(msg)

    def get_alive_players(self) -> list[Player]:
        return [p for p in self.players if p.is_alive]

    # ──────────────────────────── SkillContext ────────────────────────────

    def _make_ctx(self) -> RoleContext:
        return RoleContext(
            players=self.players,
            night_state=self.night_state,
            seer_results=self.seer_results,
            round=self.round,
        )

    # ──────────────────────────── 游戏流程 ────────────────────────────

    async def start_game(
        self, roster: list[str], config: dict, preset_name: str = ""
    ) -> tuple[bool, str]:
        if self.phase != GamePhase.WAITING:
            return False, "游戏已在进行中"
        if len(self.players) != len(roster):
            return False, f"玩家数量 ({len(self.players)}) 与角色数量 ({len(roster)}) 不符"

        self.load_config(config)
        self._preset_name = preset_name
        role_map = build_role_map(config.get("roles", []))

        shuffled = roster.copy()
        random.shuffle(shuffled)
        for player, role_name in zip(
            sorted(self.players, key=lambda p: p.seat), shuffled, strict=True
        ):
            raw = role_map.get(role_name)
            if raw is None:
                return False, f"配置中未定义角色: {role_name}"
            player.role = build_role_from_config(raw)
            player.is_alive = True
            player.can_vote = True
            player.is_sheriff = False

        self.events.log("game_start", f"游戏开始，共 {len(self.players)} 名玩家")

        for player in self.players:
            await self.cm.send_to_seat(
                player.seat,
                {"type": "your_info", "data": player.to_private_dict()},
            )

        self._game_task = asyncio.create_task(self._game_loop())
        return True, "游戏开始"

    def _check_force_night(self) -> bool:
        """检查是否有强制进入黑夜指令，有则清除标志并返回 True"""
        if self._force_night:
            self._force_night = False
            return True
        return False

    async def _game_loop(self):
        try:
            while True:
                self.round += 1
                logger.info("===== 第 %d 轮 开始 =====", self.round)
                await self._run_night()
                if self._check_force_night():
                    continue

                if self.round == 1:
                    await self._run_sheriff_election()
                    if self._check_force_night():
                        continue

                game_over = await self._run_day()
                if game_over:
                    return
                if self._check_force_night():
                    continue

        except asyncio.CancelledError:
            logger.info("游戏循环已取消")
        except Exception:
            logger.exception("游戏循环异常")

    # ──────────────────────────── 私信辅助 ────────────────────────────

    async def _broadcast_your_info(self):
        """向每位有角色的玩家发送 your_info，警长额外注入警长技能"""
        for player in self.players:
            if not player.role:
                continue
            data = player.to_private_dict()
            if player.is_sheriff and data["role"] is not None:
                # 注入警长特有技能供前端渲染
                data["role"] = {
                    **data["role"],
                    "skills": [
                        {
                            "display_name": "警长",
                            "description": "投票权重 1.5 票，出局时可移交或销毁警徽",
                        }
                    ],
                }
            await self.cm.send_to_seat(player.seat, {"type": "your_info", "data": data})

    # ──────────────────────────── 警长竞选 ────────────────────────────

    async def _run_sheriff_election(self):
        """完整警长竞选流程：报名 → 竞选发言 → 投票"""
        self.phase = GamePhase.SHERIFF
        self.events.log("phase_change", "竞选警长开始")
        await self._broadcast_notification("天亮了，开始竞选警长", audio="开始竞选警长.mp3")

        # ── 阶段一：报名 ──────────────────────────────────────────────
        self.current_action = "竞选警长 · 请报名或跳过"
        await self._broadcast_game_state()

        alive_seats = [p.seat for p in self.get_alive_players()]
        self._pending_future = asyncio.get_event_loop().create_future()
        self._pending_seats = set(alive_seats)
        self._pending_votes = {}

        for seat in alive_seats:
            await self.cm.send_to_seat(
                seat,
                {
                    "type": "action_request",
                    "data": {
                        "skill": "sheriff_nominate",
                        "skill_display": "竞选警长",
                        "message": "请选择：参与竞选警长，或跳过",
                        "valid_targets": [1, 0],
                        "requires_target": True,
                        "can_skip": False,
                        "is_group": False,
                        "options": {"1": "参与竞选", "0": "跳过竞选"},
                    },
                },
            )

        with suppress(asyncio.TimeoutError):
            await asyncio.wait_for(self._pending_future, timeout=120.0)

        nominees = [seat for seat, val in self._pending_votes.items() if val == 1]
        self._pending_future = None
        self._pending_seats = set()
        self._pending_votes = {}

        for seat in alive_seats:
            await self.cm.send_to_seat(seat, {"type": "action_clear"})

        if not nominees:
            self.events.log("sheriff_skip", "无人竞选警长，跳过警长环节")
            await self._broadcast_notification("无人竞选警长，本局无警长")
            await self._broadcast_game_state()
            return

        nominee_names = "、".join(
            f"{s} 号 {self.get_player_by_seat(s).nickname}" for s in sorted(nominees)
        )
        await self._broadcast_notification(f"竞选警长：{nominee_names}")
        self.events.log("sheriff_nominees", f"竞选者：{nominees}")

        # ── 阶段二：竞选发言（随机顺序） ─────────────────────────────
        self.current_action = "竞选警长 · 竞选发言"
        await self._broadcast_game_state()

        shuffled_nominees = nominees.copy()
        random.shuffle(shuffled_nominees)

        for seat in shuffled_nominees:
            if self._force_night:
                break
            player = self.get_player_by_seat(seat)
            self.current_speaker_seat = seat
            self.current_action = f"竞选警长 · {seat} 号 {player.nickname} 发言"
            await self._broadcast_game_state()
            self.events.log("speech_start", f"{seat} 号 {player.nickname} 竞选发言", {"seat": seat})
            await self._wait_speech_end(seat, timeout=180)

        self.current_speaker_seat = None

        # ── 阶段三：投票 ──────────────────────────────────────────────
        self.current_action = "竞选警长 · 请投票"
        self.voting_candidates = nominees
        await self._broadcast_notification(
            f"请投票选出警长（候选：{nominee_names}）", audio="开始投票.mp3"
        )
        await self._broadcast_game_state()

        eligible_voters = [p.seat for p in self.get_alive_players() if p.seat not in nominees]
        votes = await self._request_vote(eligible_voters, nominees, timeout=120.0)
        self.voting_candidates = []

        tally: dict[int, float] = {}
        for _voter, target in votes.items():
            if isinstance(target, int) and target in nominees:
                tally[target] = tally.get(target, 0) + 1

        if not tally:
            self.events.log("sheriff_no_vote", "无人投票，无警长")
            await self._broadcast_notification("无人投票，本局无警长")
            await self._broadcast_game_state()
            return

        tally_strs = [
            f"{s}号({self.get_player_by_seat(s).nickname}){int(v)}票"
            for s, v in sorted(tally.items())
        ]
        await self._broadcast_notification("票型：" + "，".join(tally_strs))

        max_votes = max(tally.values())
        top = [s for s, v in tally.items() if v == max_votes]

        if len(top) > 1:
            await self._broadcast_notification(
                f"平票！{' 和 '.join(str(s) + '号' for s in top)} 均无效，本局无警长"
            )
            self.events.log("sheriff_tie", f"警长平票：{top}")
            await self._broadcast_game_state()
            return

        sheriff_seat = top[0]
        sheriff = self.get_player_by_seat(sheriff_seat)
        if sheriff:
            sheriff.is_sheriff = True
            self.events.log("sheriff_elected", f"{sheriff_seat} 号 {sheriff.nickname} 当选警长")
            await self._broadcast_notification(
                f"{sheriff_seat} 号 {sheriff.nickname} 当选警长！",
                audio=[self._seat_audio(sheriff_seat), "当选警长.mp3"],
            )

        await self._broadcast_game_state()
        await self._broadcast_your_info()

    # ──────────────────────────── 夜晚（配置驱动） ────────────────────────────

    async def _run_night(self):
        self.phase = GamePhase.NIGHT
        self.night_state = NightState()
        self.current_speaker_seat = None
        self.current_action = f"第 {self.round} 夜 · 天黑请闭眼"
        self.events.log("phase_change", f"第 {self.round} 夜开始")
        await self._broadcast_notification(
            f"第 {self.round} 夜降临，天黑请闭眼", audio="天黑请闭眼.mp3"
        )
        await self._broadcast_game_state()

        # 按配置的 night actions 顺序执行（角色名列表）
        for role_name in self._night_actions:
            # 先取得角色实例，再用 isinstance 判断类型（防止硬编码角色名）
            role_players = self._get_role_players(role_name)
            role = role_players[0][1] if role_players else self._get_role_template(role_name)

            if isinstance(role, WitchRole):
                await self._run_witch_night()
                continue

            if not role_players:
                # 维持流程：无人（死亡或不存在），播睁眼/闭眼保持节奏
                if role:
                    await self._broadcast_role_phase(role, has_players=False)
                continue

            await self._run_role_action(role_players, role)

    def _get_role_players(self, role_name: str) -> list[tuple[Player, Role]]:
        """找出拥有指定角色的所有玩家（含死亡，死亡时也要广播睁眼/闭眼）"""
        return [
            (player, player.role)
            for player in self.players
            if player.role and player.role.name == role_name
        ]

    def _get_role_template(self, role_name: str) -> Optional[Role]:
        """从角色配置中构建一个临时 Role 实例，用于获取音频/消息配置"""
        raw_role = self._role_map.get(role_name)
        if not raw_role:
            return None
        return build_role_from_config(raw_role)

    async def _broadcast_role_phase(self, role: Role, has_players: bool):
        """广播角色的睁眼/闭眼公告（无论是否有人能用，保持节奏）"""
        if role.open_msg:
            self.current_action = role.open_msg
            self.events.log("night_action", role.open_msg)
            await self._broadcast_notification(role.open_msg, audio=role.open_audio, wait=True)
            await self._broadcast_game_state()

        if not has_players and role.close_msg:
            # 无玩家时仍播放执行操作音频保持节奏，不等待前端信号
            if role.action_audio:
                await self.cm.broadcast_audio(role.action_audio)
            self.current_action = role.close_msg
            self.events.log("night_action", role.close_msg)
            await self._broadcast_notification(role.close_msg, audio=role.close_audio, wait=True)
            await self._broadcast_game_state()

    async def _run_role_action(self, role_players: list[tuple[Player, Role]], role: Role):
        """执行一组同角色的夜晚行动"""
        ctx = self._make_ctx()
        able_players = [p for p, r in role_players if p.is_alive and r.can_use(p, ctx)]

        # 广播睁眼
        await self._broadcast_role_phase(role, has_players=bool(able_players))

        if not able_players:
            return

        if role.action_audio:
            await self.cm.broadcast_audio(role.action_audio)

        seats = [p.seat for p in able_players]
        results = await self._request_action(seats, role, message=role.display_name)

        ctx = self._make_ctx()  # 重新获取，可能有状态更新

        if role.name == "狼人":
            votes = [v for v in results.values() if v is not None]
            if votes:
                target = Counter(votes).most_common(1)[0][0]
                result = role.execute(able_players[0], target, ctx)
                self.events.log(
                    "role_action", result.message, {"role": role.name, "target": target}
                )
        else:
            for seat, target in results.items():
                player = self.get_player_by_seat(seat)
                if not player:
                    continue
                role_inst = next((r for p, r in role_players if p.seat == seat), role)
                if target is not None or not role_inst.requires_target():
                    result = role_inst.execute(player, target, ctx)
                    self.events.log(
                        "role_action",
                        result.message,
                        {"role": role.name, "seat": seat, "target": target},
                    )
                    if role.name == "预言家" and result.success and target is not None:
                        target_p = ctx.get_player_by_seat(target)
                        if target_p:
                            self.seer_results.setdefault(seat, []).append(
                                {"seat": target, "camp": target_p.team}
                            )
                            await self.cm.send_to_seat(
                                seat,
                                {
                                    "type": "seer_results",
                                    "data": {"results": self.seer_results[seat]},
                                },
                            )
                    await self.cm.send_to_seat(
                        seat,
                        {
                            "type": "action_result",
                            "data": {"message": result.message, "success": result.success},
                        },
                    )

        # 广播闭眼
        if role.close_msg:
            self.current_action = role.close_msg
            self.events.log("night_action", role.close_msg)
            await self._broadcast_notification(role.close_msg, audio=role.close_audio, wait=True)
            await self._broadcast_game_state()

    async def _run_witch_night(self):
        """女巫夜晚：解药+毒药合并为一次交互"""
        # 从角色配置取 open/close 配置
        witch_tmpl = self._get_role_template("女巫")
        open_msg = witch_tmpl.open_msg if witch_tmpl else "女巫请睁眼"
        open_audio = witch_tmpl.open_audio if witch_tmpl else None
        action_audio = witch_tmpl.action_audio if witch_tmpl else None
        close_msg = witch_tmpl.close_msg if witch_tmpl else "女巫请闭眼"
        close_audio = witch_tmpl.close_audio if witch_tmpl else None

        if open_msg:
            self.current_action = open_msg
            self.events.log("night_action", open_msg)
            await self._broadcast_notification(open_msg, audio=open_audio, wait=True)
            await self._broadcast_game_state()

        witch_players = [p for p in self.players if p.is_alive and p.role and p.role.name == "女巫"]

        if not witch_players:
            # 女巫已死时播放执行操作音频保持节奏，不等待前端信号
            if action_audio:
                await self.cm.broadcast_audio(action_audio)
            if close_msg:
                self.current_action = close_msg
                self.events.log("night_action", close_msg)
                await self._broadcast_notification(close_msg, audio=close_audio)
                await self._broadcast_game_state()
            return

        if action_audio:
            await self.cm.broadcast_audio(action_audio)

        seats = [p.seat for p in witch_players]
        witch_results = await self._request_witch_action(seats)

        ctx = self._make_ctx()
        for seat, actions in witch_results.items():
            player = self.get_player_by_seat(seat)
            if not player or not isinstance(player.role, WitchRole):
                continue
            witch_role = player.role

            save_target = actions.get("save")
            if save_target is not None and witch_role.can_save(player, ctx):
                result = witch_role.execute_save(player, save_target, ctx)
                self.events.log(
                    "role_action",
                    result.message,
                    {"role": "女巫解药", "seat": seat, "target": save_target},
                )
                await self.cm.send_to_seat(
                    seat,
                    {
                        "type": "action_result",
                        "data": {"message": result.message, "success": result.success},
                    },
                )

            poison_target = actions.get("poison")
            if poison_target is not None and witch_role.can_poison(player, ctx):
                result = witch_role.execute_poison(player, poison_target, ctx)
                self.events.log(
                    "role_action",
                    result.message,
                    {"role": "女巫毒药", "seat": seat, "target": poison_target},
                )
                await self.cm.send_to_seat(
                    seat,
                    {
                        "type": "action_result",
                        "data": {"message": result.message, "success": result.success},
                    },
                )

        if close_msg:
            self.current_action = close_msg
            self.events.log("night_action", close_msg)
            await self._broadcast_notification(close_msg, audio=close_audio, wait=True)
            await self._broadcast_game_state()

    # ──────────────────────────── 黎明 ────────────────────────────

    async def _process_night_deaths(self) -> list[int]:
        """处理夜晚死亡：计算死亡名单、播报公告、触发死亡技能。"""
        self.current_action = f"第 {self.round} 夜 · 黎明公告"
        self.events.log("phase_change", f"第 {self.round} 夜 黎明")
        deaths: list[int] = []

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
            if self._check_win():
                await self._broadcast_notification(f"昨夜，{names} 死亡")
            else:
                death_audio = [self._seat_audio(s) for s in deaths] + ["玩家出局.mp3"]
                await self._broadcast_notification(
                    f"昨夜，{names} 死亡", audio=death_audio, wait=True
                )
            for s in deaths:
                await self._run_badge_transfer(s)
                await self._handle_on_death(s, cause="night_kill")
        else:
            await self._broadcast_notification("昨夜平安，没有玩家死亡")

        return deaths

    # ──────────────────────────── 白天 ────────────────────────────

    async def _run_day(self) -> bool:
        """白天完整流程：黎明公告 → 讨论发言 → 投票。返回 True 表示游戏已结束。"""
        self.phase = GamePhase.DAY_DISCUSS
        self.events.log("phase_change", f"第 {self.round} 天白天")

        # ── 黎明公告 ──────────────────────────────────────────────────────
        deaths = await self._process_night_deaths()
        await self._broadcast_game_state()

        if self._check_force_night():
            return False

        winner = self._check_win()
        if winner:
            await self._end_game(winner)
            return True

        # ── 发言 ──────────────────────────────────────────────────────────
        await asyncio.sleep(3)
        self.current_action = f"第 {self.round} 天 · 请依次发言"
        await self._broadcast_notification(
            f"第 {self.round} 天，天亮了，请依次发言", audio="天亮了请睁眼.mp3"
        )
        await self._broadcast_game_state()

        alive = sorted(self.get_alive_players(), key=lambda p: p.seat)
        sheriff = next((p for p in self.players if p.is_alive and p.is_sheriff), None)
        if deaths and alive:
            start = min(deaths) + 1
        elif alive:
            start = alive[0].seat
        else:
            start = 1

        if sheriff and alive:
            direction = await self._request_sheriff_direction(sheriff)
            others = [p for p in alive if p.seat != sheriff.seat]
            if direction == 2:
                ordered_others = sorted(others, key=lambda p: (sheriff.seat - p.seat) % 100)
            else:
                ordered_others = sorted(others, key=lambda p: (p.seat - sheriff.seat) % 100)
            reordered = [*ordered_others, sheriff]
        else:
            reordered = sorted(alive, key=lambda p: (p.seat - start) % 100)

        for player in reordered:
            if self._force_night:
                break
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
        if self._check_force_night():
            return False

        # ── 投票 ──────────────────────────────────────────────────────────
        self.phase = GamePhase.DAY_VOTE
        self.current_action = "投票阶段 · 请选择淘汰玩家"
        self.voting_candidates = [p.seat for p in self.get_alive_players()]
        await self._broadcast_notification("请投票选择淘汰玩家（0 表示弃票）", audio="开始投票.mp3")
        await self._broadcast_game_state()

        await self._run_vote()

        winner = self._check_win()
        if winner:
            await self._end_game(winner)
            return True

        await self._broadcast_game_state()
        return False

    async def _run_vote(self) -> Optional[int]:
        eligible_voters = [p for p in self.get_alive_players() if p.can_vote]
        candidates = [p.seat for p in self.get_alive_players()]
        self.voting_candidates = candidates

        votes = await self._request_vote(
            [p.seat for p in eligible_voters], candidates, timeout=120.0
        )

        tally = defaultdict(float)
        for voter_seat, target_seat in votes.items():
            if target_seat and target_seat in candidates:
                voter = self.get_player_by_seat(voter_seat)
                weight = 1.5 if voter and voter.is_sheriff else 1
                tally[target_seat] += weight

        self.voting_candidates = []

        if tally:
            tally_strs = []
            for s in sorted(tally.keys()):
                p = self.get_player_by_seat(s)
                name = p.nickname if p else "?"
                v = tally[s]
                tally_strs.append(
                    f"{s}号({name}){v:.0f}票" if v == int(v) else f"{s}号({name}){v}票"
                )
            await self._broadcast_notification("票型：" + "，".join(tally_strs))
        else:
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
                f"{loser_seat} 号 {loser.nickname} 被淘汰！",
                audio=[self._seat_audio(loser_seat), "玩家出局.mp3"],
                wait=True,
            )
            await self._broadcast_game_state()
            await self._run_badge_transfer(loser_seat)
            await self._handle_on_death(loser_seat, cause="vote")
            # 白痴翻牌后 is_alive=True，跳过遗言
            if not self._check_win() and not loser.is_alive:
                await self._run_last_words(loser_seat)
        return loser_seat

    async def _run_last_words(self, seat: int):
        player = self.get_player_by_seat(seat)
        if not player:
            return
        self.current_speaker_seat = seat
        self.current_action = f"{seat} 号 {player.nickname} 发表遗言"
        await self._broadcast_notification(
            f"{seat} 号 {player.nickname} 请发表遗言", audio="发表遗言.mp3"
        )
        await self._broadcast_game_state()
        await self._wait_speech_end(seat, timeout=120)
        self.current_speaker_seat = None

    # ──────────────────────────── 警徽移交 ────────────────────────────

    async def _run_badge_transfer(self, dying_seat: int):
        """警长出局时，请求其移交或销毁警徽"""
        player = self.get_player_by_seat(dying_seat)
        if not player or not player.is_sheriff:
            return

        alive_others = [p.seat for p in self.get_alive_players() if p.seat != dying_seat]
        self._pending_future = asyncio.get_event_loop().create_future()
        self._pending_seats = {dying_seat}
        self._pending_votes = {}

        await self.cm.send_to_seat(
            dying_seat,
            {
                "type": "action_request",
                "data": {
                    "skill": "sheriff_badge_transfer",
                    "skill_display": "移交/销毁警徽",
                    "message": "你是警长，请选择移交警徽给某位玩家，或选择0销毁警徽",
                    "valid_targets": [0, *alive_others],
                    "requires_target": True,
                    "can_skip": False,
                    "is_group": False,
                    "options": {"0": "销毁警徽"},
                },
            },
        )

        try:
            await asyncio.wait_for(self._pending_future, timeout=60.0)
        except asyncio.TimeoutError:
            logger.warning("警徽移交超时，默认销毁")

        target = self._pending_votes.get(dying_seat)
        self._pending_future = None
        self._pending_seats = set()
        self._pending_votes = {}
        await self.cm.send_to_seat(dying_seat, {"type": "action_clear"})

        for p in self.players:
            p.is_sheriff = False

        if isinstance(target, int) and target != 0:
            new_sheriff = next((p for p in self.players if p.seat == target), None)
            if new_sheriff:
                new_sheriff.is_sheriff = True
                self.events.log("badge_transfer", f"警徽移交给 {target} 号 {new_sheriff.nickname}")
                await self._broadcast_notification(
                    f"警徽移交给 {target} 号 {new_sheriff.nickname}", audio="移交警徽.mp3"
                )
            else:
                self.events.log("badge_destroyed", "警徽已销毁")
                await self._broadcast_notification("警徽已销毁", audio="选择撕除警徽.mp3")
        else:
            self.events.log("badge_destroyed", "警徽已销毁")
            await self._broadcast_notification("警徽已销毁", audio="选择撕除警徽.mp3")

        await self._broadcast_game_state()
        await self._broadcast_your_info()

    # ──────────────────────────── 死亡技能 ────────────────────────────

    async def _handle_on_death(self, seat: int, cause: str = "vote"):
        player = self.get_player_by_seat(seat)
        if not player or not player.role:
            return
        ctx = self._make_ctx()
        role = player.role

        # 根据死因过滤可触发的阶段
        _night_phases = {RolePhase.ON_NIGHT_KILL, RolePhase.ON_DEATH}
        _vote_phases = {RolePhase.ON_VOTE_OUT, RolePhase.ON_DEATH}
        allowed = _night_phases if cause == "night_kill" else _vote_phases
        if role.phase not in allowed or not role.can_use(player, ctx):
            return

        if not role.requires_target():
            result = role.execute(player, None, ctx)
            await self._broadcast_notification(result.message)
            if result.success:
                await self._broadcast_game_state()
            return

        trigger_audio = role.open_audio
        await self._broadcast_notification(
            f"{seat} 号 {player.nickname} 触发技能：{role.display_name}",
            audio=trigger_audio,
        )
        results = await self._request_action([seat], role, message="请选择目标", timeout=60.0)
        target = results.get(seat)
        if isinstance(target, int):
            ctx = self._make_ctx()
            result = role.execute(player, target, ctx)
            await self._broadcast_notification(result.message)
            await self._broadcast_game_state()
            for s in result.affected_seats:
                killed = self.get_player_by_seat(s)
                if killed and killed.is_alive:
                    killed.is_alive = False
                    self.events.log(
                        "player_died", f"{s} 号 {killed.nickname} 被技能击杀", {"seat": s}
                    )
                    await self._run_badge_transfer(s)
                    await self._handle_on_death(s, cause=cause)

    # ──────────────────────────── 胜负判断 ────────────────────────────

    def _check_win(self) -> Optional[str]:
        alive = self.get_alive_players()
        wolves = [p for p in alive if p.team == "狼人"]
        villagers = [p for p in alive if p.team != "狼人"]
        if not wolves:
            return "村民"
        if len(wolves) >= len(villagers):
            return "狼人"
        return None

    async def _end_game(self, winner: str):
        self.phase = GamePhase.GAME_OVER
        self.winner = winner
        label = "狼人" if winner == "狼人" else "好人"
        faction_audio = "狼人.mp3" if winner == "狼人" else "好人玩家.mp3"
        self.events.log("game_over", f"游戏结束，{label}阵营胜利")
        await self._broadcast_notification(
            f"游戏结束！{label}阵营胜利！", audio=[faction_audio, "阵营胜利.mp3"]
        )
        await self._broadcast_game_state(reveal_roles=True)

    # ──────────────────────────── 动作收集 ────────────────────────────

    async def _request_action(
        self,
        seats: list[int],
        role: Role,
        message: str = "",
        timeout: float = 120.0,
    ) -> dict[int, Optional[int]]:
        ctx = self._make_ctx()

        self._pending_future = asyncio.get_event_loop().create_future()
        self._pending_seats = set(seats)
        self._pending_votes = {}

        for seat in seats:
            player = self.get_player_by_seat(seat)
            valid_targets = role.get_valid_targets(player, ctx) if player else []
            await self.cm.send_to_seat(
                seat,
                {
                    "type": "action_request",
                    "data": {
                        "skill": role.name,
                        "skill_display": role.display_name,
                        "message": message or f"请行动：{role.display_name}",
                        "valid_targets": valid_targets,
                        "requires_target": role.requires_target(),
                        "can_skip": role.can_skip(),
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

        for seat in seats:
            await self.cm.send_to_seat(seat, {"type": "action_clear"})

        return result

    async def _request_witch_action(
        self, seats: list[int], timeout: float = 120.0
    ) -> dict[int, dict | int | None]:
        self._pending_future = asyncio.get_event_loop().create_future()
        self._pending_seats = set(seats)
        self._pending_votes = {}

        ctx = self._make_ctx()
        kill_target = self.night_state.kill_target
        for seat in seats:
            player = self.get_player_by_seat(seat)
            if not player or not isinstance(player.role, WitchRole):
                continue
            witch_role = player.role
            can_save = witch_role.can_save(player, ctx)
            can_poison = witch_role.can_poison(player, ctx)
            poison_targets = (
                [p.seat for p in ctx.get_alive_players() if p.seat != seat] if can_poison else []
            )
            await self.cm.send_to_seat(
                seat,
                {
                    "type": "witch_request",
                    "data": {
                        "kill_target": kill_target,
                        "can_save": can_save,
                        "can_poison": can_poison,
                        "poison_targets": poison_targets,
                    },
                },
            )

        try:
            await asyncio.wait_for(self._pending_future, timeout=timeout)
        except asyncio.TimeoutError:
            logger.warning("女巫动作超时，座位：%s", seats)

        result = dict(self._pending_votes)
        self._pending_future = None
        self._pending_seats = set()
        self._pending_votes = {}

        for seat in seats:
            await self.cm.send_to_seat(seat, {"type": "action_clear"})

        return result

    async def _request_vote(
        self, voter_seats: list[int], candidates: list[int], timeout: float = 120.0
    ) -> dict[int, int | dict | None]:
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
        # 播放 xx号发言音频
        await self.cm.broadcast_audio([self._seat_audio(seat), "发言.mp3"])

        self._pending_future = asyncio.get_event_loop().create_future()
        self._pending_seats = {seat}
        self._pending_votes = {}

        await self.cm.send_to_seat(
            seat,
            {"type": "speech_turn", "data": {"message": "请发言，发言结束后点击「发言结束」"}},
        )

        with suppress(asyncio.TimeoutError):
            await asyncio.wait_for(self._pending_future, timeout=timeout)

        self._pending_future = None
        self._pending_seats = set()
        self._pending_votes = {}

    async def _request_sheriff_direction(self, sheriff: Player, timeout: float = 60.0) -> int:
        """请求警长选择发言方向：1=右手（顺序），2=左手（逆序）"""
        self.current_action = f"警长 {sheriff.seat} 号选择发言方向"
        await self._broadcast_game_state()
        self._pending_future = asyncio.get_event_loop().create_future()
        self._pending_seats = {sheriff.seat}
        self._pending_votes = {}

        await self.cm.send_to_seat(
            sheriff.seat,
            {
                "type": "action_request",
                "data": {
                    "skill": "sheriff_direction",
                    "skill_display": "警长指定发言顺序",
                    "message": "请选择发言方向",
                    "valid_targets": [1, 2],
                    "requires_target": True,
                    "can_skip": False,
                    "is_group": False,
                    "options": {"1": "右手发言", "2": "左手发言"},
                },
            },
        )

        try:
            await asyncio.wait_for(self._pending_future, timeout=timeout)
        except asyncio.TimeoutError:
            logger.warning("警长发言方向选择超时，默认右手")

        result = self._pending_votes.get(sheriff.seat, 1)
        self._pending_future = None
        self._pending_seats = set()
        self._pending_votes = {}

        await self.cm.send_to_seat(sheriff.seat, {"type": "action_clear"})
        return result if result in (1, 2) else 1

    # ──────────────────────────── 外部提交接口 ────────────────────────────

    def submit_action(self, seat: int, target: Optional[int]) -> bool:
        if seat not in self._pending_seats:
            return False
        self._pending_votes[seat] = target
        self._check_pending_complete()
        return True

    def submit_witch_action(self, seat: int, save: Optional[int], poison: Optional[int]) -> bool:
        if seat not in self._pending_seats:
            return False
        self._pending_votes[seat] = {"save": save, "poison": poison}
        self._check_pending_complete()
        return True

    def submit_speech_end(self, seat: int) -> bool:
        if seat not in self._pending_seats:
            return False
        self._pending_votes[seat] = None
        self._check_pending_complete()
        return True

    def _check_pending_complete(self):
        if (
            self._pending_future
            and not self._pending_future.done()
            and self._pending_seats.issubset(set(self._pending_votes.keys()))
        ):
            self._pending_future.set_result(self._pending_votes)

    # ──────────────────────────── 广播辅助 ────────────────────────────

    _SEAT_AUDIO_MAP: ClassVar[dict[int, str]] = {
        1: "一号.mp3",
        2: "二号.mp3",
        3: "三号.mp3",
        4: "四号.mp3",
        5: "五号.mp3",
        6: "六号.mp3",
        7: "七号.mp3",
        8: "八号.mp3",
        9: "九号.mp3",
        10: "十号.mp3",
        11: "十一号.mp3",
        12: "十二号.mp3",
        13: "十三号.mp3",
        14: "十四号.mp3",
        15: "十五号.mp3",
    }

    def _seat_audio(self, seat: int) -> str:
        return self._SEAT_AUDIO_MAP.get(seat, f"{seat}号.mp3")

    async def _broadcast_notification(self, message: str, audio=None, wait: bool = False):
        self.events.log("notification", message)
        await self.cm.broadcast({"type": "notification", "data": {"message": message}})
        if audio:
            files = audio if isinstance(audio, list) else [audio]
            await self.cm.broadcast_audio(files, wait=wait)

    async def _broadcast_game_state(self, reveal_roles: bool = False):
        state = self._build_public_state(reveal_roles)
        await self.cm.broadcast({"type": "game_state", "data": state})

    def _build_public_state(self, reveal_roles: bool = False) -> GameStateData:
        return {
            "phase": self.phase.value,
            "phase_display": PHASE_DISPLAY.get(self.phase, self.phase.value),
            "current_action": self.current_action,
            "round": self.round,
            "winner": self.winner,
            "current_speaker_seat": self.current_speaker_seat,
            "voting_candidates": self.voting_candidates,
            "preset_name": self._preset_name,
            "audio_device_seat": self.cm.audio_device_seat,
            "players": [
                {
                    **p.to_public_dict(),
                    "role_display": (p.role.display_name if p.role else None)
                    if reveal_roles
                    else None,
                }
                for p in sorted(self.players, key=lambda x: x.seat)
            ],
        }

    def get_public_state(self) -> GameStateData:
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
        await self._broadcast_your_info()
        return f"{seat} 号已被设为警长" if player else "玩家不存在"

    async def admin_skip_phase(self) -> str:
        if self._pending_future and not self._pending_future.done():
            self._pending_future.set_result({})
            return "已跳过当前步骤"
        return "当前无等待步骤"

    async def admin_reset(self) -> str:
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
        self.seer_results = {}
        self._role_map = {}
        self._night_actions = []
        self._preset_name = ""
        self._force_night = False
        self.events.log("admin_reset", "管理员重置游戏")
        # 通知所有客户端清除动作面板、恢复初始态
        for seat in self.cm.get_connected_seats():
            await self.cm.send_to_seat(seat, {"type": "action_clear"})
        await self._broadcast_game_state()
        return "游戏已重置"

    async def admin_goto_night(self) -> str:
        """强制结束当前所有等待并进入下一轮黑夜"""
        self._force_night = True
        self.current_action = ""
        self.current_speaker_seat = None
        self.voting_candidates = []
        # 解锁当前所有等待
        if self._pending_future and not self._pending_future.done():
            self._pending_future.set_result({})
        await self._broadcast_game_state()
        return "已强制进入黑夜"

    def admin_rollback(self, event_id: int) -> str:
        removed = self.events.truncate_after(event_id)
        return f"已删除 {len(removed)} 条日志记录（游戏状态需手动修正）"


# ──────────────────────────── 全局单例 ────────────────────────────

_game_instance: Optional[Game] = None


def get_game() -> Game:
    if _game_instance is None:
        msg = "Game未初始化"
        raise RuntimeError(msg)
    return _game_instance


def init_game(cm: "ConnectionManager") -> Game:
    global _game_instance
    _game_instance = Game(cm)
    return _game_instance
