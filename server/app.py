"""FastAPI 应用：HTTP 路由 + WebSocket 消息处理"""

import json
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

from .config import list_presets, load_preset
from .connection_manager import manager
from .game import Game, get_game, init_game

logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent.parent
TEMPLATE_PATH = BASE_DIR / "templates" / "index.html"


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_game(manager)
    logger.info("狼人杀助手已启动")
    yield


app = FastAPI(title="狼人杀助手", lifespan=lifespan)

app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")


# ──────────────────────────── HTTP 路由 ────────────────────────────


@app.get("/", response_class=HTMLResponse)
async def index():
    return TEMPLATE_PATH.read_text(encoding="utf-8")


@app.get("/api/presets")
async def get_presets():
    """返回所有可用预设列表"""
    return {"presets": list_presets()}


@app.get("/api/state")
async def get_state():
    return get_game().get_public_state()


@app.get("/api/events")
async def get_events():
    return {"events": get_game().events.get_all()}


# ──────────────────────────── WebSocket ────────────────────────────


@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    await manager.accept(ws)
    game = get_game()

    await manager.send_to_ws(ws, {"type": "game_state", "data": game.get_public_state()})

    try:
        while True:
            raw = await ws.receive_text()
            try:
                msg = json.loads(raw)
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
        await manager.broadcast({"type": "game_state", "data": game.get_public_state()})


async def _handle_message(ws: WebSocket, msg: dict, game: Game):
    msg_type = msg.get("type")
    data = msg.get("data", {})

    if msg_type == "join":
        seat = data.get("seat")
        nickname = (data.get("nickname") or "").strip()
        if not isinstance(seat, int) or seat < 1 or not nickname:
            await manager.send_to_ws(
                ws, {"type": "error", "data": {"message": "座位号和昵称不能为空"}}
            )
            return

        success, message, is_reconnect = game.add_or_update_player(seat, nickname)
        if success:
            manager.bind(seat, ws)
        await manager.send_to_ws(
            ws, {"type": "join_result", "data": {"success": success, "message": message}}
        )
        if success:
            player = game.get_player_by_seat(seat)
            if player and player.role:
                await manager.send_to_seat(
                    seat, {"type": "your_info", "data": player.to_private_dict()}
                )
            await manager.broadcast({"type": "game_state", "data": game.get_public_state()})

    elif msg_type == "action":
        seat = manager.get_seat_by_ws(ws)
        if seat is None:
            return
        ok = game.submit_action(seat, data.get("target"))
        if not ok:
            await manager.send_to_ws(
                ws, {"type": "error", "data": {"message": "当前不是你的行动时机"}}
            )

    elif msg_type == "vote":
        seat = manager.get_seat_by_ws(ws)
        if seat is None:
            return
        ok = game.submit_action(seat, data.get("target"))
        if not ok:
            await manager.send_to_ws(ws, {"type": "error", "data": {"message": "当前不是投票阶段"}})

    elif msg_type == "witch_action":
        seat = manager.get_seat_by_ws(ws)
        if seat is not None:
            ok = game.submit_witch_action(seat, data.get("save"), data.get("poison"))
            if not ok:
                await manager.send_to_ws(
                    ws, {"type": "error", "data": {"message": "当前不是女巫行动时机"}}
                )

    elif msg_type == "speech_end":
        seat = manager.get_seat_by_ws(ws)
        if seat is not None:
            game.submit_speech_end(seat)

    elif msg_type == "audio_done":
        seat = manager.get_seat_by_ws(ws)
        if seat is not None:
            manager.notify_audio_done(seat)

    elif msg_type == "admin":
        await _handle_admin(ws, data, game)

    else:
        logger.debug("[WS] 未知消息类型：%s", msg_type)


async def _handle_admin(ws: WebSocket, data: dict, game: Game):
    command = data.get("command")
    result_msg = "未知命令"

    if command == "start_game":
        preset_name = data.get("preset")
        custom_roster = data.get("roles")

        if preset_name:
            try:
                config = load_preset(preset_name)
            except FileNotFoundError as e:
                await manager.send_to_ws(ws, {"type": "error", "data": {"message": str(e)}})
                return
            roster = config.get("roster", [])
        elif custom_roster:
            # 自定义角色列表时必须同时提供配置或使用默认配置
            # 这里要求同时提供 preset 基础配置名，否则无法获得 role 定义
            base_preset = data.get("base_preset")
            if not base_preset:
                await manager.send_to_ws(
                    ws,
                    {
                        "type": "error",
                        "data": {"message": "自定义角色列表需要提供 base_preset 参数"},
                    },
                )
                return
            try:
                config = load_preset(base_preset)
            except FileNotFoundError as e:
                await manager.send_to_ws(ws, {"type": "error", "data": {"message": str(e)}})
                return
            roster = custom_roster
        else:
            await manager.send_to_ws(
                ws, {"type": "error", "data": {"message": "请提供 preset 或 roles"}}
            )
            return

        _success, result_msg = await game.start_game(roster, config, preset_name=preset_name or "")

    elif command == "skip_phase":
        result_msg = await game.admin_skip_phase()

    elif command == "force_kill":
        result_msg = await game.admin_force_kill(data["seat"])

    elif command == "force_revive":
        result_msg = await game.admin_force_revive(data["seat"])

    elif command == "set_sheriff":
        result_msg = await game.admin_set_sheriff(data["seat"])

    elif command == "reset_game":
        result_msg = await game.admin_reset()

    elif command == "goto_night":
        result_msg = await game.admin_goto_night()

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

    await manager.send_to_ws(ws, {"type": "admin_result", "data": {"message": result_msg}})
    await manager.broadcast({"type": "game_state", "data": game.get_public_state()})
