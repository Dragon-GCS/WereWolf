"""玩家模块"""

from typing import Literal, Optional

from server.messages import PublicPlayerDict, YourInfoData

from .roles import Role


class Player:
    """玩家类"""

    def __init__(self, seat: int, nickname: str):
        self.seat = seat
        self.nickname = nickname
        self.role: Optional[Role] = None
        self.is_alive: bool = True
        self.can_vote: bool = True
        self.is_sheriff: bool = False

    @property
    def team(self) -> Literal["狼人", "好人"]:
        if not self.role:
            raise ValueError("玩家没有分配角色")
        return "狼人" if self.role.team == "狼人" else "好人"

    def to_public_dict(self) -> PublicPlayerDict:
        """公开信息（广播给所有玩家）"""
        return {
            "seat": self.seat,
            "nickname": self.nickname,
            "is_alive": self.is_alive,
            "is_sheriff": self.is_sheriff,
            "role_display": self.role.display_name if not self.is_alive and self.role else None,
        }

    def to_private_dict(self) -> YourInfoData:
        """私密信息（仅发给本人）"""
        return {
            "seat": self.seat,
            "nickname": self.nickname,
            "is_alive": self.is_alive,
            "is_sheriff": self.is_sheriff,
            "can_vote": self.can_vote,
            "role": self.role.to_dict() if self.role else None,
        }
