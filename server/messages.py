"""服务端 → 客户端 WebSocket 消息类型定义"""

from typing import Literal, Optional, TypedDict

# ── 子结构 ──────────────────────────────────────────────


class SkillDict(TypedDict):
    name: str
    display_name: str
    phase: str
    priority: int
    can_skip: bool


class RoleDict(TypedDict):
    name: str
    display_name: str
    team: Literal["狼人", "村民", "神职", "中立"]
    description: str
    skills: list[SkillDict]


class PublicPlayerDict(TypedDict):
    seat: int
    nickname: str
    is_alive: bool
    is_sheriff: bool
    role_display: Optional[str]


class PrivatePlayerDict(TypedDict):
    seat: int
    nickname: str
    is_alive: bool
    is_sheriff: bool
    can_vote: bool
    role: Optional[RoleDict]


class SeerResultItem(TypedDict):
    seat: int
    camp: Literal["狼人", "好人"]


# ── 消息 data 结构 ────────────────────────────────────────


class GameStateData(TypedDict):
    phase: str
    phase_display: str
    current_action: Optional[str]
    round: int
    winner: Optional[str]
    current_speaker_seat: Optional[int]
    voting_candidates: list[int]
    preset_name: str
    audio_device_seat: Optional[int]
    players: list[PublicPlayerDict]


class ErrorData(TypedDict):
    message: str


class JoinResultData(TypedDict):
    success: bool
    message: str


class YourInfoData(TypedDict):
    seat: int
    nickname: str
    is_alive: bool
    is_sheriff: bool
    can_vote: bool
    role: Optional[RoleDict]


class ActionRequestData(TypedDict):
    skill: str
    skill_display: str
    message: str
    valid_targets: list[int]
    requires_target: bool
    can_skip: bool
    is_group: bool
    options: Optional[dict]  # {value: label} 若有则用标签替代座位号显示


class WitchRequestData(TypedDict):
    kill_target: Optional[int]
    can_save: bool
    can_poison: bool
    poison_targets: list[int]


class VoteRequestData(TypedDict):
    message: str
    candidates: list[int]


class SpeechTurnData(TypedDict):
    message: str


class ActionResultData(TypedDict):
    message: str
    success: bool


class SeerResultsData(TypedDict):
    results: list[SeerResultItem]


class NotificationData(TypedDict):
    message: str


class AdminResultData(TypedDict):
    message: str


class PlayAudioData(TypedDict):
    files: list[str]


# ── 完整消息类型 ──────────────────────────────────────────


class GameStateMsg(TypedDict):
    type: Literal["game_state"]
    data: GameStateData


class ErrorMsg(TypedDict):
    type: Literal["error"]
    data: ErrorData


class JoinResultMsg(TypedDict):
    type: Literal["join_result"]
    data: JoinResultData


class YourInfoMsg(TypedDict):
    type: Literal["your_info"]
    data: YourInfoData


class ActionRequestMsg(TypedDict):
    type: Literal["action_request"]
    data: ActionRequestData


class ActionClearMsg(TypedDict):
    type: Literal["action_clear"]


class WitchRequestMsg(TypedDict):
    type: Literal["witch_request"]
    data: WitchRequestData


class VoteRequestMsg(TypedDict):
    type: Literal["vote_request"]
    data: VoteRequestData


class SpeechTurnMsg(TypedDict):
    type: Literal["speech_turn"]
    data: SpeechTurnData


class ActionResultMsg(TypedDict):
    type: Literal["action_result"]
    data: ActionResultData


class SeerResultsMsg(TypedDict):
    type: Literal["seer_results"]
    data: SeerResultsData


class NotificationMsg(TypedDict):
    type: Literal["notification"]
    data: NotificationData


class AdminResultMsg(TypedDict):
    type: Literal["admin_result"]
    data: AdminResultData


class PlayAudioMsg(TypedDict):
    type: Literal["play_audio"]
    data: PlayAudioData


# 所有服务端消息的联合类型
ServerMessage = (
    GameStateMsg
    | ErrorMsg
    | JoinResultMsg
    | YourInfoMsg
    | ActionRequestMsg
    | ActionClearMsg
    | WitchRequestMsg
    | VoteRequestMsg
    | SpeechTurnMsg
    | ActionResultMsg
    | SeerResultsMsg
    | NotificationMsg
    | AdminResultMsg
    | PlayAudioMsg
)
