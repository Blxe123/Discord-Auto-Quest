"""
Quest REST layer — pure aiohttp (ไม่พึ่ง discord.py-self)
ครอบ endpoint ของ Discord Quest ที่ reverse มาแล้ว:
  GET  /quests/@me
  POST /quests/{id}/enroll          body {"location": 0}
  POST /quests/{id}/video-progress  body {"timestamp": n}     (WATCH_VIDEO — headless ได้)
  POST /quests/{id}/heartbeat       body {"stream_key","terminal"} (PLAY_ACTIVITY)
"""
from __future__ import annotations

import asyncio
import base64
import json
from datetime import datetime, timezone
from dataclasses import dataclass, field
from typing import Optional

import aiohttp

API = "https://discord.com/api/v10"

# super-properties แบบ desktop client (เผื่อ endpoint บางตัวเช็ค)
_DESKTOP_SP = base64.b64encode(json.dumps({
    "os": "Windows", "browser": "Discord Client", "release_channel": "stable",
    "client_version": "1.0.9201", "os_version": "10.0.26100", "os_arch": "x64",
    "app_arch": "x64", "system_locale": "en-US", "client_build_number": 569167,
    "native_build_number": 64502, "client_event_source": None,
}, separators=(",", ":")).encode()).decode()

# ⚠️ สำคัญ: heartbeat ของ PLAY_ON_DESKTOP จะ 401/40001 ถ้า UA ไม่มี "Electron/..."
# (Discord เช็คแค่ตรงนี้ ไม่ใช่ attestation — ใส่ Electron แล้ว farm เกม headless ได้)
_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
       "discord/1.0.9201 Chrome/128.0.6613.186 Electron/32.2.7 Safari/537.36")

SUPPORTED = ("WATCH_VIDEO", "WATCH_VIDEO_ON_MOBILE", "PLAY_ON_DESKTOP",
             "STREAM_ON_DESKTOP", "PLAY_ACTIVITY")
HEADLESS_VIDEO = ("WATCH_VIDEO", "WATCH_VIDEO_ON_MOBILE")


@dataclass
class Quest:
    id: str
    name: str
    task_type: str
    target: int
    progress: int
    enrolled: bool
    completed: bool
    expires: datetime
    app_id: str

    @property
    def headless_farmable(self) -> bool:
        return self.task_type in HEADLESS_VIDEO

    @property
    def game_farmable(self) -> bool:
        return self.task_type == "PLAY_ON_DESKTOP"

    def pct(self) -> int:
        return min(100, int(self.progress * 100 / self.target)) if self.target else 0


class QuestError(Exception):
    def __init__(self, status: int, code, message: str):
        super().__init__(f"{status}/{code}: {message}")
        self.status, self.code, self.message = status, code, message


class QuestAccount:
    """หนึ่ง user-token = หนึ่ง account worker (REST อย่างเดียว)"""

    def __init__(self, token: str, session: aiohttp.ClientSession):
        self.token = token
        self.session = session
        self.username: str = "?"
        self.user_id: str = ""

    # ── low-level ────────────────────────────────────────────
    async def _req(self, method: str, path: str, body: Optional[dict] = None) -> dict:
        headers = {
            "Authorization": self.token,
            "X-Super-Properties": _DESKTOP_SP,
            "Content-Type": "application/json",
            "User-Agent": _UA,
        }
        async with self.session.request(
            method, API + path, headers=headers,
            data=json.dumps(body) if body is not None else None,
            timeout=aiohttp.ClientTimeout(total=30),
        ) as r:
            txt = await r.text()
            data = json.loads(txt) if txt and txt[0] in "{[" else {}
            if r.status >= 400:
                raise QuestError(r.status, data.get("code"), data.get("message", txt[:120]))
            return data

    # ── high-level ───────────────────────────────────────────
    async def login(self) -> bool:
        try:
            me = await self._req("GET", "/users/@me")
            self.username = me.get("username", "?")
            self.user_id = me.get("id", "")
            return True
        except Exception:
            return False

    async def list_quests(self, include_expired: bool = False) -> list[Quest]:
        d = await self._req("GET", "/quests/@me")
        now = datetime.now(timezone.utc)
        out: list[Quest] = []
        for q in d.get("quests", []):
            cfg = q["config"]
            tasks = cfg.get("task_config_v2", {}).get("tasks", {})
            ttype = next((t for t in SUPPORTED if t in tasks), None)
            if not ttype:
                continue
            try:
                exp = datetime.fromisoformat(cfg["expires_at"])
            except Exception:
                exp = now
            if not include_expired and exp <= now:
                continue
            st = q.get("user_status") or {}
            prog_node = (st.get("progress") or {}).get(ttype) or {}
            out.append(Quest(
                id=q["id"],
                name=cfg["application"]["name"],
                task_type=ttype,
                target=int(tasks[ttype]["target"]),
                progress=int(prog_node.get("value", st.get("stream_progress_seconds", 0) or 0)),
                enrolled=bool(st.get("enrolled_at")),
                completed=bool(st.get("completed_at")),
                expires=exp,
                app_id=str(cfg["application"]["id"]),
            ))
        return out

    async def enroll(self, quest_id: str) -> None:
        await self._req("POST", f"/quests/{quest_id}/enroll", {"location": 0})

    async def video_progress(self, quest_id: str, timestamp: float) -> dict:
        return await self._req("POST", f"/quests/{quest_id}/video-progress",
                               {"timestamp": timestamp})

    async def heartbeat(self, quest_id: str, stream_key, terminal: bool) -> dict:
        return await self._req("POST", f"/quests/{quest_id}/heartbeat",
                               {"stream_key": stream_key, "terminal": terminal})

    # ── farm WATCH_VIDEO (headless) ──────────────────────────
    async def farm_video(self, q: Quest, on_progress=None) -> bool:
        """credit video quest จนครบ — server จำกัด timestamp <= เวลาจริงหลัง enroll"""
        if not q.enrolled:
            await self.enroll(q.id)
        done = q.progress
        step = 7
        while done < q.target:
            await asyncio.sleep(min(step, q.target - done))
            ts = min(q.target, done + step + 0.5)
            res = await self.video_progress(q.id, ts)
            done = min(q.target, done + step)
            if on_progress:
                await on_progress(done, q.target)
            if res.get("completed_at"):
                return True
        # ปิดท้ายให้ครบ
        res = await self.video_progress(q.id, q.target)
        return bool(res.get("completed_at")) or done >= q.target

    # ── farm PLAY_ON_DESKTOP (headless ผ่าน heartbeat) ───────
    async def farm_game(self, q: Quest, on_progress=None) -> bool:
        """credit เควสเกมจนครบ — heartbeat ทุก ~90s, server นับเวลาจริง (cap 120s/ครั้ง)
        ต้องมี Electron ใน UA ไม่งั้น 401/40001 (ดู _UA)"""
        if not q.enrolled:
            await self.enroll(q.id)
        stream_key = f"call:{q.id}:1"
        done = q.progress
        stalls = 0
        while done < q.target:
            res = await self.heartbeat(q.id, stream_key, terminal=False)
            if res.get("completed_at"):
                return True
            node = (res.get("progress") or {}).get("PLAY_ON_DESKTOP") or {}
            new = int(node.get("value", done))
            stalls = stalls + 1 if new <= done else 0
            done = new
            if on_progress:
                await on_progress(done, q.target)
            if done >= q.target or stalls >= 6:   # stall>=6 (~9 นาทีไม่ขยับ) = เลิก
                break
            await asyncio.sleep(min(90, max(5, q.target - done)))
        res = await self.heartbeat(q.id, stream_key, terminal=True)
        return bool(res.get("completed_at")) or done >= q.target
