"""FastAPI 应用：HTTP 路由 + WebSocket 消息处理"""

import logging
from contextlib import asynccontextmanager
from pathlib import Path

import yaml
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

from .connection_manager import manager
from .game import get_game, init_game

logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent.parent
TEMPLATE_PATH = BASE_DIR / "templates" / "index.html"
CONFIG_PATH = BASE_DIR / "config" / "roles.yml"


def _load_config() -> dict:
    with CONFIG_PATH.open(encoding="utf-8") as f:
        return yaml.safe_load(f)


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_game(manager)
    logger.info("狼人杀助手已启动")
    yield


app = FastAPI(title="狼人杀助手", lifespan=lifespan)

# 挂载静态资源
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")


# ──────────────────────────── HTTP 路由 ────────────────────────────


@app.get("/", response_class=HTMLResponse)
async def index():
    return TEMPLATE_PATH.read_text(encoding="utf-8")


@app.get("/api/config")
async def get_config():
    """返回角色预设配置"""
    return _load_config()


@app.get("/api/state")
async def get_state():
    game = get_game()
    return game.get_public_state()


@app.get("/api/events")
async def get_events():
    game = get_game()
    return {"events": game.events.get_all()}


# ──────────────────────────── WebSocket ────────────────────────────


@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    await manager.accept(ws)
    game = get_game()

    # 发送初始游戏状态
    await manager.send_to_ws(
        ws,
        {
            "type": "game_state",
            "data": game.get_public_state(),
        },
    )

    try:
        while True:
            raw = await ws.receive_text()
            try:
                import json as _j

                msg = _j.loads(raw)
            except Exception:
                logger.warning("[WS] 非法 JSON")
                continue

            await _handle_message(ws, msg, game)

    except WebSocketDisconnect:
        seat = manager.disconnect_by_ws(ws)
        if seat is not None:
            player = game.get_player_by_seat(seat)
            if player:
                logger.info("[断开] %d 号 %s 离线", player.seat, player.nickname)
        await manager.broadcast(
            {
                "type": "game_state",
                "data": game.get_public_state(),
            }
        )


async def _handle_message(ws: WebSocket, msg: dict, game):
    msg_type = msg.get("type")
    data = msg.get("data", {})

    # ── 加入游戏 ──
    if msg_type == "join":
        seat = data.get("seat")
        nickname = (data.get("nickname") or "").strip()
        if not isinstance(seat, int) or seat < 1 or not nickname:
            await manager.send_to_ws(
                ws,
                {
                    "type": "error",
                    "data": {"message": "座位号和昵称不能为空"},
                },
            )
            return

        success, message, is_reconnect = game.add_or_update_player(seat, nickname)
        # 无论是新加入还是重连，绑定 ws -> seat
        if success:
            manager.bind(seat, ws)
        await manager.send_to_ws(
            ws,
            {
                "type": "join_result",
                "data": {"success": success, "message": message},
            },
        )
        if success:
            # 重连时重新发送私密角色信息
            player = game.get_player_by_seat(seat)
            if player and player.role:
                await manager.send_to_seat(
                    seat,
                    {
                        "type": "your_info",
                        "data": player.to_private_dict(),
                    },
                )
            await manager.broadcast(
                {
                    "type": "game_state",
                    "data": game.get_public_state(),
                }
            )

    # ── 技能操作 ──
    elif msg_type == "action":
        target = data.get("target")
        seat = manager.get_seat_by_ws(ws)
        if seat is None:
            return
        ok = game.submit_action(seat, target)
        if not ok:
            await manager.send_to_ws(
                ws,
                {
                    "type": "error",
                    "data": {"message": "当前不是你的行动时机"},
                },
            )

    # ── 投票 ──
    elif msg_type == "vote":
        target = data.get("target")
        seat = manager.get_seat_by_ws(ws)
        if seat is None:
            return
        ok = game.submit_action(seat, target)
        if not ok:
            await manager.send_to_ws(
                ws,
                {
                    "type": "error",
                    "data": {"message": "当前不是投票阶段"},
                },
            )

    # ── 发言结束 ──
    elif msg_type == "speech_end":
        seat = manager.get_seat_by_ws(ws)
        if seat is not None:
            game.submit_speech_end(seat)

    # ── 管理员命令 ──
    elif msg_type == "admin":
        await _handle_admin(ws, data, game)

    else:
        logger.debug("[WS] 未知消息类型：%s", msg_type)


async def _handle_admin(ws: WebSocket, data: dict, game):
    command = data.get("command")
    result_msg = "未知命令"

    if command == "start_game":
        preset = data.get("preset")
        custom = data.get("roles")
        if preset:
            config = _load_config()
            presets_list = config.get("presets", [])
            matched = next((p for p in presets_list if p.get("name") == preset), None)
            if not matched:
                await manager.send_to_ws(
                    ws,
                    {
                        "type": "error",
                        "data": {"message": f"预设 '{preset}' 不存在"},
                    },
                )
                return
            role_list = matched.get("roles", [])
        elif custom:
            role_list = custom
        else:
            await manager.send_to_ws(
                ws,
                {
                    "type": "error",
                    "data": {"message": "请提供预设名称或角色列表"},
                },
            )
            return

        _success, result_msg = await game.start_game(role_list)

    elif command == "skip_phase":
        result_msg = await game.admin_skip_phase()

    elif command == "force_kill":
        result_msg = await game.admin_force_kill(data.get("seat"))

    elif command == "force_revive":
        result_msg = await game.admin_force_revive(data.get("seat"))

    elif command == "set_sheriff":
        result_msg = await game.admin_set_sheriff(data.get("seat"))

    elif command == "reset_game":
        result_msg = await game.admin_reset()

    elif command == "rollback":
        event_id = data.get("event_id")
        if isinstance(event_id, int):
            result_msg = game.admin_rollback(event_id)
        else:
            result_msg = "请提供有效的事件 ID"

    elif command == "set_audio_device":
        seat = data.get("seat")
        manager.audio_device_seat = seat if isinstance(seat, int) else None
        result_msg = f"音频设备已设为 {seat} 号" if seat else "音频设备已设为广播模式"

    await manager.send_to_ws(
        ws,
        {
            "type": "admin_result",
            "data": {"message": result_msg},
        },
    )
    await manager.broadcast(
        {
            "type": "game_state",
            "data": game.get_public_state(),
        }
    )
