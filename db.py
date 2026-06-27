"""
Postgres layer + token encryption สำหรับ quest bot (multi-user)

ตาราง:
  accounts     หนึ่งแถว = หนึ่ง token ที่ผู้ใช้ลงทะเบียน (token เก็บแบบเข้ารหัส)
  completions  log เควสที่ทำเสร็จ (ใช้ทำ leaderboard + กันนับซ้ำ)
"""
from __future__ import annotations

import os
from typing import Optional

import asyncpg
from cryptography.fernet import Fernet

# อ่าน env แบบ lazy (ตอนใช้จริง) ไม่ใช่ตอน import — กันปัญหา load_dotenv() มาทีหลัง
_fernet_cached: Optional[Fernet] = None


def _fernet() -> Fernet:
    global _fernet_cached
    if _fernet_cached is None:
        key = os.getenv("ENCRYPTION_KEY", "")
        if not key:
            raise RuntimeError("ENCRYPTION_KEY ไม่ได้ตั้ง")
        _fernet_cached = Fernet(key.encode())
    return _fernet_cached


def encrypt(token: str) -> str:
    return _fernet().encrypt(token.encode()).decode()


def decrypt(blob: str) -> str:
    return _fernet().decrypt(blob.encode()).decode()


SCHEMA = """
CREATE TABLE IF NOT EXISTS accounts (
    id              BIGSERIAL   PRIMARY KEY,
    discord_user_id BIGINT      NOT NULL,                 -- คนที่ลงทะเบียน (เจ้าของ token)
    discord_name    TEXT        NOT NULL DEFAULT '?',
    token_enc       TEXT        NOT NULL,                 -- token เข้ารหัส Fernet
    account_user_id TEXT        NOT NULL UNIQUE,          -- id ของบัญชี Discord ที่ token นี้เป็น
    username        TEXT        NOT NULL DEFAULT '?',
    active          BOOLEAN     NOT NULL DEFAULT TRUE,
    last_error      TEXT,
    added_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS completions (
    id           BIGSERIAL   PRIMARY KEY,
    account_id   BIGINT      NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    quest_id     TEXT        NOT NULL,
    quest_name   TEXT        NOT NULL,
    completed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (account_id, quest_id)
);
CREATE TABLE IF NOT EXISTS panels (
    message_id BIGINT      PRIMARY KEY,    -- ข้อความแผงที่ปักไว้ (ไว้ auto-refresh สถิติ)
    channel_id BIGINT      NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS user_prefs (
    discord_user_id  BIGINT  PRIMARY KEY,
    presence_enabled BOOLEAN NOT NULL DEFAULT TRUE   -- โชว์เควสบนโปรไฟล์ตัวเองไหม (รายคน)
);
"""


class DB:
    def __init__(self) -> None:
        self.pool: Optional[asyncpg.Pool] = None

    async def connect(self) -> None:
        url = os.getenv("DATABASE_URL", "")
        if not url:
            raise RuntimeError("DATABASE_URL ไม่ได้ตั้ง")
        self.pool = await asyncpg.create_pool(url, min_size=1, max_size=5)
        async with self.pool.acquire() as c:
            await c.execute(SCHEMA)

    async def close(self) -> None:
        if self.pool:
            await self.pool.close()

    # ── accounts ────────────────────────────────────────────────
    async def add_account(self, discord_user_id: int, discord_name: str,
                          token: str, account_user_id: str, username: str) -> str:
        """เพิ่ม/อัปเดต token  → คืน 'added' หรือ 'updated'"""
        enc = encrypt(token)
        async with self.pool.acquire() as c:
            exists = await c.fetchval(
                "SELECT id FROM accounts WHERE account_user_id=$1", account_user_id)
            if exists:
                await c.execute(
                    "UPDATE accounts SET token_enc=$1, discord_user_id=$2, discord_name=$3, "
                    "username=$4, active=TRUE, last_error=NULL WHERE account_user_id=$5",
                    enc, discord_user_id, discord_name, username, account_user_id)
                return "updated"
            await c.execute(
                "INSERT INTO accounts (discord_user_id, discord_name, token_enc, "
                "account_user_id, username) VALUES ($1,$2,$3,$4,$5)",
                discord_user_id, discord_name, enc, account_user_id, username)
            return "added"

    async def list_user_accounts(self, discord_user_id: int) -> list[asyncpg.Record]:
        async with self.pool.acquire() as c:
            return await c.fetch(
                "SELECT id, username, account_user_id, active, last_error "
                "FROM accounts WHERE discord_user_id=$1 ORDER BY added_at", discord_user_id)

    async def all_active_accounts(self) -> list[asyncpg.Record]:
        async with self.pool.acquire() as c:
            return await c.fetch(
                "SELECT id, discord_user_id, token_enc, username, account_user_id "
                "FROM accounts WHERE active=TRUE")

    async def remove_account(self, account_id: int, discord_user_id: int) -> bool:
        async with self.pool.acquire() as c:
            res = await c.execute(
                "DELETE FROM accounts WHERE id=$1 AND discord_user_id=$2",
                account_id, discord_user_id)
        return res.endswith("1")  # "DELETE 1" = ลบสำเร็จ

    async def mark_error(self, account_id: int, err: str) -> None:
        async with self.pool.acquire() as c:
            await c.execute(
                "UPDATE accounts SET active=FALSE, last_error=$1 WHERE id=$2", err, account_id)

    # ── completions / leaderboard ───────────────────────────────
    async def record_completion(self, account_id: int, quest_id: str, quest_name: str) -> bool:
        """คืน True ถ้าเป็นการบันทึกใหม่ (ยังไม่เคยนับ)"""
        async with self.pool.acquire() as c:
            res = await c.execute(
                "INSERT INTO completions (account_id, quest_id, quest_name) "
                "VALUES ($1,$2,$3) ON CONFLICT DO NOTHING", account_id, quest_id, quest_name)
        return res.endswith("1")  # "INSERT 0 1" = แถวใหม่

    async def count_for_account(self, account_id: int) -> int:
        async with self.pool.acquire() as c:
            return await c.fetchval(
                "SELECT COUNT(*) FROM completions WHERE account_id=$1", account_id)

    async def leaderboard(self, limit: int = 10) -> list[asyncpg.Record]:
        """อันดับรายคน (รวมทุก token ของคนนั้น)"""
        async with self.pool.acquire() as c:
            return await c.fetch(
                "SELECT a.discord_user_id, MAX(a.discord_name) AS name, "
                "       COUNT(c.id) AS total, COUNT(DISTINCT a.id) AS accounts "
                "FROM accounts a LEFT JOIN completions c ON c.account_id=a.id "
                "GROUP BY a.discord_user_id ORDER BY total DESC, accounts DESC LIMIT $1", limit)

    async def user_rank(self, discord_user_id: int) -> tuple[int | None, int, int]:
        """คืน (อันดับ, จำนวนเควส, จำนวนคนทั้งหมด) ของผู้ใช้คนนี้"""
        async with self.pool.acquire() as c:
            rows = await c.fetch(
                "SELECT a.discord_user_id, COUNT(c.id) AS total "
                "FROM accounts a LEFT JOIN completions c ON c.account_id=a.id "
                "GROUP BY a.discord_user_id ORDER BY total DESC")
        for i, r in enumerate(rows):
            if r["discord_user_id"] == discord_user_id:
                return i + 1, int(r["total"]), len(rows)
        return None, 0, len(rows)

    async def global_stats(self) -> asyncpg.Record:
        """สถิติรวมทั้งระบบ"""
        async with self.pool.acquire() as c:
            return await c.fetchrow(
                "SELECT (SELECT COUNT(*) FROM accounts WHERE active) AS accounts, "
                "       (SELECT COUNT(DISTINCT discord_user_id) FROM accounts) AS users, "
                "       (SELECT COUNT(*) FROM completions) AS quests")

    # ── panels (ไว้ auto-refresh) ───────────────────────────────
    async def add_panel(self, channel_id: int, message_id: int) -> None:
        async with self.pool.acquire() as c:
            await c.execute(
                "INSERT INTO panels (message_id, channel_id) VALUES ($1,$2) "
                "ON CONFLICT (message_id) DO NOTHING", message_id, channel_id)

    async def all_panels(self) -> list[asyncpg.Record]:
        async with self.pool.acquire() as c:
            return await c.fetch("SELECT message_id, channel_id FROM panels")

    async def remove_panel(self, message_id: int) -> None:
        async with self.pool.acquire() as c:
            await c.execute("DELETE FROM panels WHERE message_id=$1", message_id)

    # ── user preferences ────────────────────────────────────────
    async def get_presence_pref(self, discord_user_id: int) -> bool:
        """โชว์ presence บนโปรไฟล์ไหม (default เปิด)"""
        async with self.pool.acquire() as c:
            v = await c.fetchval(
                "SELECT presence_enabled FROM user_prefs WHERE discord_user_id=$1", discord_user_id)
        return True if v is None else bool(v)

    async def set_presence_pref(self, discord_user_id: int, enabled: bool) -> None:
        async with self.pool.acquire() as c:
            await c.execute(
                "INSERT INTO user_prefs (discord_user_id, presence_enabled) VALUES ($1,$2) "
                "ON CONFLICT (discord_user_id) DO UPDATE SET presence_enabled=$2",
                discord_user_id, enabled)
