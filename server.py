import json
import os
import re
import secrets
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path
from random import randint
from typing import List

import requests
from fastapi import FastAPI, Header
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

BASE = Path(__file__).parent
IMAGES_DIR = BASE / "images"
IMAGES_DIR.mkdir(parents=True, exist_ok=True)

# Секреты читаются из файла .env рядом с server.py (его никому не показывайте):
#   BOT_TOKEN=токен_от_BotFather
#   CHAT_ID=ваш_chat_id
#   BOT_USERNAME=username_бота_без_@   (необязательно, иначе узнаем сами через getMe)
env = BASE / ".env"
if env.is_file():
    for line in env.read_text(encoding="utf-8").splitlines():
        if "=" in line and not line.strip().startswith("#"):
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip("\"'"))
BOT_TOKEN = os.getenv("BOT_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")
API = f"https://api.telegram.org/bot{BOT_TOKEN}"

# ───────────────────────── Подтверждение через Telegram ─────────────────────────
LOCK = threading.RLock()
SESS_FILE = BASE / "sessions.json"   # подтверждённые сессии (не публикуйте этот файл)
SESSION_TTL = 30 * 86400             # сессия живёт 30 дней
CODE_TTL = 600                       # ссылка на вход живёт 10 минут
SESSIONS = {}   # session_token -> {id, tg, name, phone, ts}
PENDING = {}    # code -> {secret, ts, user}
AWAIT = {}      # telegram user id -> code (ждём от него номер)
_bot_username = os.getenv("BOT_USERNAME", "").lstrip("@")

try:
    SESSIONS = json.loads(SESS_FILE.read_text(encoding="utf-8"))
except Exception:
    SESSIONS = {}


def save_sessions():
    tmp = SESS_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(SESSIONS, ensure_ascii=False), encoding="utf-8")
    tmp.replace(SESS_FILE)


def bot_username():
    global _bot_username
    if not _bot_username and BOT_TOKEN:
        try:
            _bot_username = requests.get(f"{API}/getMe", timeout=10).json()["result"]["username"]
        except Exception as e:
            print("getMe не удался:", type(e).__name__)
    return _bot_username


def send(chat_id, text, reply_markup=None):
    data = {"chat_id": chat_id, "text": text}
    if reply_markup:
        data["reply_markup"] = reply_markup
    try:
        requests.post(f"{API}/sendMessage", json=data, timeout=15)
    except Exception as e:
        print("Ошибка ответа боту:", type(e).__name__)


def handle(m):
    if not m or (m.get("chat") or {}).get("type") != "private":
        return
    frm = m.get("from") or {}
    uid = frm.get("id")
    chat = m["chat"]["id"]
    text = (m.get("text") or "").strip()
    contact = m.get("contact")

    if text.startswith("/start"):
        parts = text.split(maxsplit=1)
        code = parts[1].strip() if len(parts) > 1 else ""
        with LOCK:
            p = PENDING.get(code)
            ok = bool(p) and not p["user"] and time.time() - p["ts"] < CODE_TTL
            if ok:
                AWAIT[uid] = code
        if ok:
            send(chat, "Чтобы подтвердить аккаунт, нажмите кнопку ниже и поделитесь своим номером телефона.",
                 {"keyboard": [[{"text": "📱 Поделиться номером", "request_contact": True}]],
                  "resize_keyboard": True, "one_time_keyboard": True})
        else:
            send(chat, "Ссылка устарела или уже использована. Вернитесь на сайт и нажмите «Подтвердить через Telegram» ещё раз.")
        return

    if contact:
        with LOCK:
            code = AWAIT.get(uid)
        if not code:
            send(chat, "Сначала откройте сайт и нажмите «Подтвердить через Telegram».")
            return
        # принимаем только СВОЙ номер: чужой контакт (пересланный или выбранный вручную) не пройдёт
        if contact.get("user_id") != uid:
            send(chat, "Нужен именно ваш номер. Нажмите кнопку «Поделиться номером» внизу.")
            return
        digits = re.sub(r"\D", "", contact.get("phone_number", ""))
        if len(digits) < 9:
            send(chat, "Не удалось прочитать номер. Попробуйте ещё раз.")
            return
        with LOCK:
            p = PENDING.get(code)
            AWAIT.pop(uid, None)
            if not p or time.time() - p["ts"] >= CODE_TTL:
                send(chat, "Время вышло. Вернитесь на сайт и начните заново.")
                return
            name = " ".join(x for x in [frm.get("first_name"), frm.get("last_name")] if x) or "Покупатель"
            p["user"] = {"id": uid, "tg": "@" + frm["username"] if frm.get("username") else "",
                         "name": name, "phone": "+" + digits}
        send(chat, "✅ Готово! Вернитесь на сайт, вход выполнится автоматически.", {"remove_keyboard": True})
        return

    send(chat, "Это бот магазина VELORI SHOP. Чтобы войти, откройте сайт и нажмите «Подтвердить через Telegram».")


def bot_loop():
    try:
        requests.post(f"{API}/deleteWebhook", timeout=15)  # getUpdates не работает при включённом вебхуке
    except Exception:
        pass
    offset = 0
    while True:
        try:
            r = requests.post(f"{API}/getUpdates", timeout=35,
                              json={"offset": offset, "timeout": 25, "allowed_updates": ["message"]}).json()
            if not r.get("ok"):
                time.sleep(5)
                continue
            for u in r["result"]:
                offset = u["update_id"] + 1
                try:
                    handle(u.get("message"))
                except Exception as e:
                    print("Ошибка обработки сообщения бота:", type(e).__name__)
        except Exception as e:
            print("Ошибка опроса Telegram:", type(e).__name__)
            time.sleep(3)


@asynccontextmanager
async def lifespan(_app):
    if BOT_TOKEN:
        threading.Thread(target=bot_loop, daemon=True).start()
    else:
        print("BOT_TOKEN не задан: вход через Telegram не заработает")
    yield


app = FastAPI(lifespan=lifespan)
app.mount("/images", StaticFiles(directory=str(IMAGES_DIR)), name="images")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


@app.get("/")
def read_index():
    return FileResponse(BASE / "index.html")


def get_session(authorization: str):
    token = authorization[7:].strip() if authorization.lower().startswith("bearer ") else ""
    with LOCK:
        s = SESSIONS.get(token)
        if s and time.time() - s["ts"] > SESSION_TTL:
            del SESSIONS[token]
            save_sessions()
            s = None
    return s


@app.post("/api/auth/start")
def auth_start():
    username = bot_username()
    if not username:
        return JSONResponse({"ok": False, "error": "bot not configured"}, status_code=503)
    code, secret, now = secrets.token_urlsafe(12), secrets.token_urlsafe(24), time.time()
    with LOCK:
        for k in [k for k, v in PENDING.items() if now - v["ts"] > CODE_TTL]:
            del PENDING[k]
        if len(PENDING) > 2000:
            return JSONResponse({"ok": False, "error": "busy"}, status_code=429)
        PENDING[code] = {"secret": secret, "ts": now, "user": None}
    return {"ok": True, "code": code, "secret": secret, "url": f"https://t.me/{username}?start={code}"}


@app.get("/api/auth/status")
def auth_status(code: str, secret: str):
    with LOCK:
        p = PENDING.get(code)
        if not p or not secrets.compare_digest(p["secret"], secret) or time.time() - p["ts"] > CODE_TTL:
            return {"ok": False, "status": "expired"}
        if not p["user"]:
            return {"ok": True, "status": "pending"}
        u = p["user"]
        del PENDING[code]
        token = secrets.token_urlsafe(32)
        SESSIONS[token] = {**u, "ts": time.time()}
        save_sessions()
    return {"ok": True, "status": "done", "session": token, "tg": u["tg"] or u["name"], "phone": u["phone"]}


@app.get("/api/auth/me")
def auth_me(authorization: str = Header(default="")):
    s = get_session(authorization)
    if not s:
        return JSONResponse({"ok": False}, status_code=401)
    return {"ok": True, "tg": s["tg"] or s["name"], "phone": s["phone"]}


@app.post("/api/auth/logout")
def auth_logout(authorization: str = Header(default="")):
    token = authorization[7:].strip() if authorization.lower().startswith("bearer ") else ""
    with LOCK:
        if SESSIONS.pop(token, None):
            save_sessions()
    return {"ok": True}


# ───────────────────────────────── Каталог и заказы ─────────────────────────────────
# Цены берутся отсюда, а не с сайта, чтобы их нельзя было подменить.
# Если меняете цену в index.html, меняйте и здесь.
PRODUCTS = {
 "1": {"n": "Поло в полоску", "p": 249000, "v": {
   "Бордовый/синий": "polo-burgundy-navy", "Бордовый/чёрный": "polo-burgundy-black",
   "Бордовый/чёрный, короткий рукав": "polo-burgundy-black-short", "Фиолетовый/чёрный": "polo-purple-black",
   "Белый/чёрный": "polo-white-black", "Синий/чёрный": "polo-navy-black"}},
 "2": {"n": "Джемпер в полоску с воротником", "p": 349000, "v": {"Чёрный/серый": "knit-polo"}},
 "3": {"n": "Водолазка", "p": 359000, "v": {"Кремовый": "turtleneck-cream", "Чёрный": "turtleneck-black"}},
 "4": {"n": "Свитшот серый с вышивкой", "p": 289000, "v": {"Серый": "sweatshirt-grey"}},
 "5": {"n": "Худи на молнии", "p": 319000, "v": {"Бордовый": "hoodie-red"}},
 "6": {"n": "Спортивный свитшот-ветровка", "p": 319000, "v": {
   "Тёмно-серый": "windbreaker-darkgrey", "Серый": "windbreaker-grey",
   "Бежевый": "windbreaker-beige", "Оливковый": "windbreaker-olive"}},
 "7": {"n": "Лонгслив вафельный", "p": 199000, "v": {"Белый": "longsleeve-white"}},
 "8": {"n": "Штаны с контрастной полосой", "p": 279000, "v": {
   "Тёмно-синий": "pants-navy", "Чёрный": "pants-black", "Чёрный/красная полоса": "pants-blackred",
   "Серый": "pants-grey", "Зелёный": "pants-green", "Бирюзовый": "pants-teal"}},
 "9": {"n": "Куртка спортивная", "p": 359000, "v": {"Чёрный/серый": "track-jacket"}},
 "10": {"n": "Куртка утеплённая с полосой", "p": 599000, "v": {
   "Чёрный/фиолетовый": "puffer-black", "Серый/синий": "puffer-grey"}},
 "11": {"n": "Очки с прозрачной оправой", "p": 149000, "v": {"Прозрачный": "glasses"}},
}


class OrderItem(BaseModel):
    id: int
    col: str = ""
    z: str
    q: int


class Order(BaseModel):
    items: List[OrderItem]
    # телефон и Telegram берутся из подтверждённого профиля, а не из запроса


@app.post("/api/order")
def create_order(order: Order, authorization: str = Header(default="")):
    s = get_session(authorization)
    if not s:
        return JSONResponse({"ok": False, "error": "auth"}, status_code=401)
    if not BOT_TOKEN or not CHAT_ID:
        print("Не заданы BOT_TOKEN или CHAT_ID в .env")
        return {"ok": False, "error": "server not configured"}

    lines, total, photo = [], 0, None
    for it in order.items[:30]:
        # ключи в PRODUCTS — строки, а id приходит числом, поэтому str(...)
        p = PRODUCTS.get(str(it.id))
        if not p or not (1 <= it.q <= 20):
            print("Пропущена позиция:", it.id, it.q)
            continue
        col = it.col if it.col in p["v"] else next(iter(p["v"]))
        sum_ = p["p"] * it.q
        total += sum_
        lines.append(f"• {p['n']}, {col}, размер {it.z[:10]} × {it.q} = {sum_:,} сум".replace(",", " "))
        if photo is None:
            f = IMAGES_DIR / (p["v"][col] + ".jpg")
            photo = f if f.is_file() else None
    if not lines:
        print("Пустой заказ: ни одна позиция не найдена")
        return {"ok": False, "error": "empty order"}

    who = s["tg"] or f"{s['name']} (ID {s['id']}, tg://user?id={s['id']})"
    text = (
        "🛍 НОВЫЙ ЗАКАЗ VELORI SHOP\n\n"
        f"✈️ Telegram: {who}  ✅ подтверждён\n📞 Телефон: {s['phone']}\n"
        + "\n" + "\n".join(lines)
        + f"\n\n💰 Итого: {total:,} сум".replace(",", " ")
    )
    try:
        if photo and len(text) <= 1024:
            with open(photo, "rb") as fh:
                r = requests.post(f"{API}/sendPhoto", data={"chat_id": CHAT_ID, "caption": text}, files={"photo": fh}, timeout=20)
        else:
            r = requests.post(f"{API}/sendMessage", json={"chat_id": CHAT_ID, "text": text}, timeout=20)
        r.raise_for_status()
        return {"ok": True, "id": f"TG-{randint(1000, 9999)}"}
    except Exception as e:
        print("Ошибка отправки в Telegram:", type(e).__name__)
        return {"ok": False, "error": "send failed"}
