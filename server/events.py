"""事件日志：记录游戏中所有事件，支持回退"""

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


@dataclass
class GameEvent:
    id: int
    event_type: str
    description: str
    timestamp: str
    data: Dict[str, Any] = field(default_factory=dict)


class EventLog:
    """游戏事件日志"""

    def __init__(self):
        self._events: List[GameEvent] = []
        self._counter: int = 0

    def log(self, event_type: str, description: str, data: Optional[dict] = None) -> GameEvent:
        self._counter += 1
        event = GameEvent(
            id=self._counter,
            event_type=event_type,
            description=description,
            timestamp=datetime.now().strftime("%H:%M:%S"),
            data=data or {},
        )
        self._events.append(event)
        logger.info("[事件 #%04d] [%s] %s", event.id, event_type, description)
        return event

    def get_all(self) -> List[dict]:
        return [
            {
                "id": e.id,
                "type": e.event_type,
                "description": e.description,
                "timestamp": e.timestamp,
            }
            for e in self._events
        ]

    def get_after(self, event_id: int) -> List[GameEvent]:
        """返回 event_id 之后的所有事件"""
        return [e for e in self._events if e.id > event_id]

    def truncate_after(self, event_id: int) -> List[GameEvent]:
        """删除 event_id 之后的所有事件，返回被删除的事件"""
        removed = [e for e in self._events if e.id > event_id]
        self._events = [e for e in self._events if e.id <= event_id]
        if removed:
            logger.info("[回退] 删除 %d 条事件（#%d 之后）", len(removed), event_id)
        return removed
