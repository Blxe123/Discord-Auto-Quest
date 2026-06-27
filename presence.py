"""
Rich Presence บนบัญชี user token (ผ่าน gateway) — โชว์ "กำลังทำเควสอะไร + progress"
บนโปรไฟล์ของเจ้าของ token เอง (ระหว่างที่บอท farm ให้)

⚠️ self-bot: บัญชีจะ "ออนไลน์ + broadcast" ตลอดที่ farm = เพิ่มความเสี่ยงแบน
   ปิดได้ด้วย env ENABLE_PRESENCE=0  (presence จะไม่ทำงาน แต่ farm ปกติ)

ออกแบบให้ "พังเงียบ" — ถ้า presence มีปัญหาอะไร ต้องไม่กระทบการ farm
"""
from __future__ import annotations

import asyncio
import json
import logging

import aiohttp

log = logging.getLogger("questbot.presence")
GATEWAY = "wss://gateway.discord.gg/?v=10&encoding=json"


class _Conn:
    """หนึ่ง gateway connection ต่อ token — คอยส่ง heartbeat + อัปเดต presence"""

    def __init__(self, token: str, session: aiohttp.ClientSession, activity: dict):
        self.token = token
        self.session = session
        self.activity: dict | None = activity
        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._seq = None
        self._hb: asyncio.Task | None = None
        self._task = asyncio.create_task(self._run())

    def _payload(self) -> dict:
        return {"since": 0, "afk": False, "status": "online",
                "activities": [self.activity] if self.activity else []}

    async def _run(self) -> None:
        while True:
            try:
                async with self.session.ws_connect(GATEWAY, max_msg_size=0, heartbeat=None) as ws:
                    self._ws = ws
                    hello = json.loads((await ws.receive()).data)
                    interval = hello["d"]["heartbeat_interval"] / 1000
                    await ws.send_str(json.dumps({"op": 2, "d": {
                        "token": self.token,
                        "properties": {"os": "Windows", "browser": "Chrome", "device": ""},
                        "presence": self._payload(), "compress": False,
                    }}))
                    self._hb = asyncio.create_task(self._heartbeat(ws, interval))
                    async for msg in ws:
                        if msg.type == aiohttp.WSMsgType.TEXT:
                            d = json.loads(msg.data)
                            if d.get("s") is not None:
                                self._seq = d["s"]
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.debug(f"presence ws ({self.token[:10]}…): {e}")
            finally:
                if self._hb:
                    self._hb.cancel()
            await asyncio.sleep(5)   # หลุด → ต่อใหม่

    async def _heartbeat(self, ws, interval: float) -> None:
        try:
            while True:
                await asyncio.sleep(interval)
                await ws.send_str(json.dumps({"op": 1, "d": self._seq}))
        except Exception:
            return

    async def update(self) -> None:
        if self._ws and not self._ws.closed:
            try:
                await self._ws.send_str(json.dumps({"op": 3, "d": self._payload()}))
            except Exception:
                pass

    async def stop(self) -> None:
        self._task.cancel()
        if self._hb:
            self._hb.cancel()
        if self._ws and not self._ws.closed:
            try:
                await self._ws.close()
            except Exception:
                pass


class PresenceManager:
    """จัดการ presence ต่อ token — เปิด connection ตอนเริ่ม farm, ปิดเมื่อ farm จบ"""

    def __init__(self, session: aiohttp.ClientSession):
        self.session = session
        self._conns: dict[str, _Conn] = {}

    @staticmethod
    def _activity(name: str, app_id: str, done: int, target: int, start_ms: int) -> dict:
        pct = int(done * 100 / target) if target else 0
        return {
            "name": name,
            "type": 0,                       # Playing
            "application_id": str(app_id),
            "details": f"กำลังทำเควส • {pct}%",
            "state": f"{done}/{target}s",
            "timestamps": {"start": start_ms},
        }

    async def set_quest(self, token: str, name: str, app_id: str,
                        done: int, target: int, start_ms: int) -> None:
        act = self._activity(name, app_id, done, target, start_ms)
        conn = self._conns.get(token)
        if conn is None:
            self._conns[token] = _Conn(token, self.session, act)   # identify ส่ง activity เลย
        else:
            conn.activity = act
            await conn.update()

    async def clear(self, token: str) -> None:
        conn = self._conns.pop(token, None)
        if conn:
            await conn.stop()

    async def close(self) -> None:
        for c in list(self._conns.values()):
            await c.stop()
        self._conns.clear()

    def active_count(self) -> int:
        return len(self._conns)
