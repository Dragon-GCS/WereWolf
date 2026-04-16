"""WebSocket 连接管理"""

import json
import logging
from typing import Dict, Optional

from fastapi import WebSocket

logger = logging.getLogger(__name__)


class ConnectionManager:
    """管理所有 WebSocket 连接，以座位号为唯一标识"""

    def __init__(self):
        # seat -> WebSocket（已加入游戏的玩家）
        self._connections: Dict[int, WebSocket] = {}
        # 指定的音频播放设备座位号（None = 广播给所有人）
        self.audio_device_seat: Optional[int] = None

    async def accept(self, ws: WebSocket) -> None:
        """接受 WebSocket 连接（尚未绑定座位）"""
        await ws.accept()
        logger.info("[连接] 新连接接入，当前在线：%d", len(self._connections))

    def bind(self, seat: int, ws: WebSocket) -> None:
        """将座位号绑定到 WebSocket"""
        old_ws = self._connections.get(seat)
        if old_ws and old_ws is not ws:
            logger.info("[座位绑定] 座位 %d 重新绑定（旧连接替换）", seat)
        self._connections[seat] = ws
        logger.info("[座位绑定] 座位 %d 已绑定，当前在线：%d", seat, len(self._connections))

    def disconnect(self, seat: int) -> None:
        """断开指定座位的连接"""
        self._connections.pop(seat, None)
        logger.info("[断开] 座位 %d 已断开，当前在线：%d", seat, len(self._connections))

    def disconnect_by_ws(self, ws: WebSocket) -> Optional[int]:
        """根据 WebSocket 对象断开连接，返回对应座位号（若存在）"""
        for seat, w in list(self._connections.items()):
            if w is ws:
                del self._connections[seat]
                logger.info("[断开] 座位 %d 已断开，当前在线：%d", seat, len(self._connections))
                return seat
        return None

    def get_seat_by_ws(self, ws: WebSocket) -> Optional[int]:
        for seat, w in self._connections.items():
            if w is ws:
                return seat
        return None

    def get_online_count(self) -> int:
        return len(self._connections)

    async def send_to_ws(self, ws: WebSocket, message: dict) -> bool:
        """直接发送给指定 WebSocket（用于加入前的响应）"""
        try:
            await ws.send_text(json.dumps(message, ensure_ascii=False))
            return True
        except Exception as e:
            logger.warning("[发送失败] ws=%s: %s", id(ws), e)
            return False

    async def send_to_seat(self, seat: int, message: dict) -> bool:
        ws = self._connections.get(seat)
        if not ws:
            return False
        try:
            await ws.send_text(json.dumps(message, ensure_ascii=False))
            return True
        except Exception as e:
            logger.warning("[发送失败] 座位 %d: %s", seat, e)
            return False

    async def broadcast(self, message: dict) -> None:
        """广播给所有已绑定座位的在线玩家"""
        payload = json.dumps(message, ensure_ascii=False)
        dead_seats = []
        for seat, ws in list(self._connections.items()):
            try:
                await ws.send_text(payload)
            except Exception:
                dead_seats.append(seat)
        for seat in dead_seats:
            self.disconnect(seat)

    async def broadcast_audio(self, audio_file: str) -> None:
        """发送音频播放指令（指定设备或广播）"""
        msg = {"type": "play_audio", "data": {"file": audio_file}}
        if self.audio_device_seat is not None:
            await self.send_to_seat(self.audio_device_seat, msg)
        else:
            await self.broadcast(msg)


# 全局单例
manager = ConnectionManager()
