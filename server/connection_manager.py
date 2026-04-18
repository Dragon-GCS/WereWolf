"""WebSocket 连接管理"""

import asyncio
import json
import logging
from typing import Optional

from fastapi import WebSocket

from .messages import PlayAudioMsg, ServerMessage

logger = logging.getLogger(__name__)


class ConnectionManager:
    """管理所有 WebSocket 连接，以座位号为唯一标识"""

    def __init__(self):
        # seat -> WebSocket（已加入游戏的玩家）
        self._connections: dict[int, WebSocket] = {}
        # 指定的音频播放设备座位号（None = 广播给所有人）
        self.audio_device_seat: Optional[int] = None
        # 等待音频播放完毕的 Future（由音频设备客户端发送 audio_done 信号触发）
        self._audio_done_future: Optional[asyncio.Future] = None

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

    def get_connected_seats(self) -> list[int]:
        return list(self._connections.keys())

    async def send_to_ws(self, ws: WebSocket, message: ServerMessage) -> bool:
        """直接发送给指定 WebSocket（用于加入前的响应）"""
        try:
            await ws.send_text(json.dumps(message, ensure_ascii=False))
        except Exception as e:
            logger.warning("[发送失败] ws=%s: %s", id(ws), e)
            return False
        return True

    async def send_to_seat(self, seat: int, message: ServerMessage) -> bool:
        ws = self._connections.get(seat)
        if not ws:
            return False
        try:
            await ws.send_text(json.dumps(message, ensure_ascii=False))
        except Exception as e:
            logger.warning("[发送失败] 座位 %d: %s", seat, e)
            return False
        return True

    async def broadcast(self, message: ServerMessage) -> None:
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

    async def broadcast_audio(
        self, audio_files: str | list[str], wait: bool = False, timeout: float = 60.0
    ) -> None:
        """发送音频播放指令。

        wait=False（默认）：发送后立即返回，不等待播放完成。
        wait=True：等待前端回传 audio_done 信号后再返回（最长 timeout 秒）。
          有指定音频设备时：只发给该设备，等待其信号。
          广播模式时：发给所有人，等待任意客户端的信号。
        """
        if isinstance(audio_files, str):
            audio_files = [audio_files]
        msg: PlayAudioMsg = {"type": "play_audio", "data": {"files": audio_files}}

        if not wait:
            if self.audio_device_seat is not None:
                await self.send_to_seat(self.audio_device_seat, msg)
            else:
                await self.broadcast(msg)
            return

        # wait=True：先创建 Future，再发消息，避免 audio_done 在 Future 创建前到达被丢弃
        if self.audio_device_seat is None and not self._connections:
            return
        self._audio_done_future = asyncio.get_event_loop().create_future()
        if self.audio_device_seat is not None:
            await self.send_to_seat(self.audio_device_seat, msg)
        else:
            await self.broadcast(msg)
        try:
            await asyncio.wait_for(self._audio_done_future, timeout=timeout)
        except asyncio.TimeoutError:
            if self.audio_device_seat is not None:
                logger.warning("[音频] 等待 audio_done 超时（seat=%d）", self.audio_device_seat)
            else:
                logger.warning("[音频] 广播模式等待 audio_done 超时")
        finally:
            self._audio_done_future = None

    def notify_audio_done(self, seat: int) -> bool:
        """音频设备播放完毕后调用，触发等待中的 Future。

        指定设备模式：只接受该设备的信号。
        广播模式：任意客户端的信号均可触发。
        """
        if self._audio_done_future is None or self._audio_done_future.done():
            return False

        # 指定设备模式：只接受该设备的信号
        if self.audio_device_seat is not None and self.audio_device_seat != seat:
            return False
        # 广播模式或匹配的指定设备：触发 Future
        self._audio_done_future.set_result(None)
        return True


# 全局单例
manager = ConnectionManager()
