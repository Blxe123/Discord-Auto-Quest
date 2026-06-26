# Discord Quest Auto-Farmer (multi-user · Railway-hostable)

แผง embed ปักในห้อง → เพื่อนๆ กดปุ่มลงทะเบียน **token ของตัวเอง** → บอทเก็บลง
**Postgres (เข้ารหัส)** แล้ว farm **เควสวิดีโอ** ให้อัตโนมัติ 24/7 + จัดอันดับว่าใครทำเยอะสุด

## ปุ่มในแผง

| ปุ่ม | ทำอะไร |
|---|---|
| ➕ **เพิ่ม Token** | เปิด modal วาง token → ตรวจสอบ → เก็บแบบเข้ารหัส แล้วเริ่ม farm ทันที |
| 📊 **สถานะของฉัน** | ดูบัญชีของตัวเอง + จำนวนเควสที่ทำ + สถานะ token |
| 🗑️ **ลบ Token** | เลือกบัญชีของตัวเองออกจากระบบ |
| 🏆 **อันดับ** | top 10 ว่าใครทำเควสเยอะสุด (นับรวมทุก token ของคนนั้น) |

> ทุกคนจัดการได้เฉพาะ token **ของตัวเอง** (ผูกกับ Discord id คนกด) · `/setup_panel` ปักแผงได้เฉพาะ `OWNER_ID`

## บอททำอะไรได้ / ไม่ได้

| Quest type | บน cloud |
|---|---|
| 📺 `WATCH_VIDEO` | ✅ **farm จบในตัวอัตโนมัติ** |
| 🖥️ `PLAY_ON_DESKTOP` | ❌ heartbeat ต้องมาจาก Discord desktop native client — REST ปลอมไม่ได้ |

---

## Setup

### 1. สร้าง Bot
1. https://discord.com/developers/applications → **New Application**
2. แท็บ **Bot** → **Reset Token** → copy = `DISCORD_BOT_TOKEN`
3. **OAuth2 → URL Generator**: scopes `bot` + `applications.commands`,
   permissions `Send Messages` + `Embed Links` → เปิด URL → invite เข้า server

### 2. id ที่ต้องใช้
- `OWNER_ID` = user id ของคุณ (Developer Mode → คลิกขวาตัวเอง → Copy User ID)
- `GUILD_ID` = คลิกขวา server → Copy Server ID (ใส่แล้ว slash โผล่ทันที)

### 3. สร้าง ENCRYPTION_KEY (ครั้งเดียว ห้ามเปลี่ยน)
```bash
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```
> เปลี่ยน key เมื่อไหร่ = decrypt token เก่าทั้งหมดไม่ได้ ทุกคนต้องลงทะเบียนใหม่

### 4. รัน local (ทดสอบ)
ต้องมี Postgres — รันผ่าน Docker ง่ายสุด:
```bash
docker run -d --name questbot-pg -e POSTGRES_PASSWORD=questbot \
  -e POSTGRES_DB=questbot -p 5432:5432 postgres:16

pip install -r requirements.txt
copy .env.example .env          # แก้ค่าใน .env (DATABASE_URL ชี้ container ข้างบน)
python bot.py
```
ใน Discord: พิมพ์ `/setup_panel` ในห้องที่อยาก → ปักหมุดแผง → กดปุ่มทดสอบ

### 5. Deploy Railway
1. push โฟลเดอร์นี้ขึ้น GitHub
2. https://railway.app → **New Project → Deploy from GitHub repo**
3. ในโปรเจกต์เดียวกัน กด **+ New → Database → Add PostgreSQL**
   (Railway จะ inject `DATABASE_URL` ให้บอทอัตโนมัติ)
4. แท็บ **Variables** ของ service บอท → ใส่ `DISCORD_BOT_TOKEN`, `OWNER_ID`,
   `ENCRYPTION_KEY`, (`GUILD_ID`)
5. Deploy → ไปที่ server → `/setup_panel`

> บอทเป็น **worker** ไม่ต้อง expose port

---

## โครงสร้าง
```
bot.py         แผงปุ่ม + modal + auto-farm loop + leaderboard + /setup_panel
quest_api.py   REST layer ของ Discord Quest (login / list / enroll / video-progress)
db.py          Postgres pool + schema + เข้ารหัส token (Fernet) + CRUD + leaderboard
```

## ตาราง DB
- `accounts` — 1 แถว = 1 token (เก็บ `token_enc` เข้ารหัส, ผูก `discord_user_id` คนลงทะเบียน)
- `completions` — log เควสที่ทำเสร็จ (กันนับซ้ำด้วย `UNIQUE(account_id, quest_id)`)

## ความเสี่ยง / ความปลอดภัย
- การ automate บัญชี Discord ผิด **ToS** — บัญชีโดนแบนถาวรได้ (ผู้ลงทะเบียนยอมรับเอง)
- คุณเป็น **ผู้ดูแล token ของเพื่อน** — token เก็บแบบเข้ารหัส แต่ถ้า `ENCRYPTION_KEY` + DB หลุดพร้อมกัน = ถอดได้ ดูแล key ให้ดี
- อย่า commit `.env` ขึ้น repo (มี `.gitignore` กันแล้ว) · ใส่ secret ใน Railway Variables เท่านั้น
