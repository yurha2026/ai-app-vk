from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
import os
import sqlite3
import requests
import secrets
import string
from contextlib import contextmanager
import uvicorn
import uuid
import json
import hashlib
import base64
import urllib.parse
import time
import re

# Импортируем dotenv только если файл существует
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

app = FastAPI(title="AI Assistant Pro")

# CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Константы (все проверяем через os.getenv с значениями по умолчанию)
DATABASE = "database.db"
FRONTEND_URL = os.getenv("FRONTEND_URL", "https://ai-app-vk.vercel.app")
BACKEND_URL = os.getenv("API_BASE", os.getenv("BACKEND_URL", "https://neuro-guru-backend.onrender.com"))
JSONBIN_KEY = os.getenv("JSONBIN_KEY", "")
JSONBIN_ID = os.getenv("JSONBIN_ID", "")
VK_CLIENT_ID = os.getenv("VK_CLIENT_ID", "54571690")
VK_CLIENT_SECRET = os.getenv("VK_CLIENT_SECRET", "")
GIGACHAT_AUTH_KEY = os.getenv("GIGACHAT_AUTH_KEY", "")
HF_TOKEN = os.getenv("HF_TOKEN", "")

# Кеши
pkce_store = {}
gigachat_token_cache = {"token": "", "expires": 0}

# Монтируем статику если есть
if os.path.exists("static"):
    app.mount("/static", StaticFiles(directory="static"), name="static")


@contextmanager
def get_db():
    conn = sqlite3.connect(DATABASE)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()


def init_db():
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS users (
                id TEXT PRIMARY KEY,
                vk_id INTEGER UNIQUE,
                email TEXT,
                name TEXT,
                photo TEXT,
                balance REAL DEFAULT 0.0,
                credits INTEGER DEFAULT 3,
                subscription_status TEXT DEFAULT 'free',
                referral_code TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS chat_history (
                id TEXT PRIMARY KEY,
                user_id TEXT,
                role TEXT,
                content TEXT,
                message_type TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS referrals (
                id TEXT PRIMARY KEY,
                referrer_id TEXT,
                referee_id TEXT,
                reward_amount REAL DEFAULT 0.0,
                status TEXT DEFAULT 'pending',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        conn.commit()
        print("✅ Database initialized")


def cloud_get_all():
    if not JSONBIN_KEY or not JSONBIN_ID:
        return {"users": {}}
    try:
        r = requests.get(
            f"https://api.jsonbin.io/v3/b/{JSONBIN_ID}/latest",
            headers={"X-Master-Key": JSONBIN_KEY},
            timeout=10
        )
        if r.status_code == 200:
            return r.json().get("record", {"users": {}})
    except Exception as e:
        print(f"Cloud read error: {e}")
    return {"users": {}}


def cloud_save_all(data):
    if not JSONBIN_KEY or not JSONBIN_ID:
        return
    try:
        requests.put(
            f"https://api.jsonbin.io/v3/b/{JSONBIN_ID}",
            headers={
                "X-Master-Key": JSONBIN_KEY,
                "Content-Type": "application/json"
            },
            json=data,
            timeout=10
        )
    except Exception as e:
        print(f"Cloud write error: {e}")


def cloud_save_user(user_data):
    data = cloud_get_all()
    vk_id = str(user_data.get("vk_id", ""))
    data["users"][vk_id] = user_data
    cloud_save_all(data)


def cloud_get_user(vk_id):
    data = cloud_get_all()
    return data.get("users", {}).get(str(vk_id))


def cloud_update_credits(vk_id, credits):
    data = cloud_get_all()
    if str(vk_id) in data.get("users", {}):
        data["users"][str(vk_id)]["credits"] = credits
        cloud_save_all(data)


def generate_pkce():
    code_verifier = secrets.token_urlsafe(64)
    code_challenge = base64.urlsafe_b64encode(
        hashlib.sha256(code_verifier.encode('ascii')).digest()
    ).decode('ascii').rstrip('=')
    return code_verifier, code_challenge


def get_gigachat_token():
    if gigachat_token_cache["token"] and gigachat_token_cache["expires"] > time.time():
        return gigachat_token_cache["token"]
    if not GIGACHAT_AUTH_KEY:
        return ""
    try:
        response = requests.post(
            "https://ngw.devices.sberbank.ru:9443/api/v2/oauth",
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Accept": "application/json",
                "RqUID": str(uuid.uuid4()),
                "Authorization": f"Basic {GIGACHAT_AUTH_KEY}"
            },
            data={"scope": "GIGACHAT_API_PERS"},
            verify=False,
            timeout=10
        )
        if response.status_code == 200:
            data = response.json()
            gigachat_token_cache["token"] = data.get("access_token", "")
            gigachat_token_cache["expires"] = time.time() + 1800
            return gigachat_token_cache["token"]
    except Exception as e:
        print(f"GigaChat token error: {e}")
    return ""


def ask_gigachat(prompt, msg_type="text"):
    token = get_gigachat_token()
    if not token:
        return None

    system_msg = "Ты опытный программист. Пиши только код." if msg_type == "code" else "Ты полезный ассистент. Отвечай по-русски."
    
    try:
        response = requests.post(
            "https://gigachat.devices.sberbank.ru/api/v1/chat/completions",
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json",
                "Authorization": f"Bearer {token}"
            },
            json={
                "model": "GigaChat",
                "messages": [
                    {"role": "system", "content": system_msg},
                    {"role": "user", "content": prompt}
                ],
                "temperature": 0.7,
                "max_tokens": 1024
            },
            verify=False,
            timeout=30
        )
        if response.status_code == 200:
            data = response.json()
            choices = data.get("choices", [])
            if choices:
                return choices[0].get("message", {}).get("content", "")
    except Exception as e:
        print(f"GigaChat error: {e}")
    return None


# ============ ROUTES ============

@app.get("/", response_class=HTMLResponse)
async def homepage():
    return """
    <!DOCTYPE html>
    <html>
    <head>
        <meta charset="UTF-8">
        <title>AI Assistant Pro</title>
        <style>
            body { font-family: Arial, sans-serif; text-align: center; padding: 50px; background: linear-gradient(135deg, #667eea 0%, #764ba2 100%); color: white; }
            h1 { font-size: 3em; margin-bottom: 10px; }
            .status { margin-top: 30px; padding: 20px; background: rgba(255,255,255,0.1); border-radius: 10px; display: inline-block; }
            a { color: #ffd700; }
        </style>
    </head>
    <body>
        <h1>🚀 Backend работает!</h1>
        <p>AI Assistant Pro API v2.0</p>
        <div class="status">
            <p>✅ Сервер активен</p>
            <p>📍 <a href="/health">Health Check</a></p>
            <p>🌐 <a href="https://ai-app-vk.vercel.app">Фронтенд</a></p>
        </div>
    </body>
    </html>
    """


@app.get("/health")
async def health():
    gc = "connected" if get_gigachat_token() else "not connected"
    cloud = "connected" if (JSONBIN_KEY and JSONBIN_ID) else "not configured"
    return {
        "status": "ok",
        "database": "sqlite",
        "gigachat": gc,
        "cloud": cloud,
        "frontend": FRONTEND_URL,
        "backend": BACKEND_URL,
        "timestamp": time.time()
    }


@app.get("/auth/vk/login")
async def vk_login_url():
    # Проверяем что BACKEND_URL определён
    callback = BACKEND_URL
    print(f"VK Callback URL: {callback}")
    
    code_verifier, code_challenge = generate_pkce()
    state = secrets.token_urlsafe(32)
    pkce_store[state] = code_verifier

    login_url = (
        f"https://id.vk.com/authorize"
        f"?response_type=code"
        f"&client_id={VK_CLIENT_ID}"
        f"&redirect_uri={callback}"
        f"&state={state}"
        f"&code_challenge={code_challenge}"
        f"&code_challenge_method=S256"
        f"&scope=vkid.personal_info"
    )
    return {"login_url": login_url}


@app.get("/auth/vk/callback")
async def vk_callback(request: Request):
    code = request.query_params.get("code")
    device_id = request.query_params.get("device_id", "")
    state = request.query_params.get("state", "")
    
    if not code:
        return HTMLResponse("<h1>Ошибка: нет кода авторизации</h1>")
    
    try:
        code_verifier = pkce_store.pop(state, secrets.token_urlsafe(64))

        # Получаем токен
        token_response = requests.post(
            "https://id.vk.com/oauth2/auth",
            data={
                "grant_type": "authorization_code",
                "client_id": VK_CLIENT_ID,
                "client_secret": VK_CLIENT_SECRET,
                "redirect_uri": BACKEND_URL,
                "code": code,
                "code_verifier": code_verifier,
                "device_id": device_id,
                "state": state
            },
            timeout=10
        )
        token_data = token_response.json()

        if "access_token" not in token_data:
            return HTMLResponse(f"<h1>Ошибка</h1><p>{json.dumps(token_data)}</p>")

        access_token = token_data["access_token"]
        user_id = token_data.get("user_id", 0)

        # Получаем данные пользователя
        user_response = requests.post(
            "https://id.vk.com/oauth2/user_info",
            data={"access_token": access_token, "client_id": VK_CLIENT_ID},
            timeout=10
        )
        user_data = user_response.json()
        user_info = user_data.get("user", {})

        name = f"{user_info.get('first_name', '')} {user_info.get('last_name', '')}".strip() or "Пользователь"
        photo = user_info.get("avatar", "")
        if not user_id:
            user_id = user_info.get("user_id", 0)

        user_uuid = str(uuid.uuid4())

        with get_db() as conn:
            cursor = conn.cursor()
            existing = cursor.execute("SELECT * FROM users WHERE vk_id = ?", (user_id,)).fetchone()

            if existing:
                cursor.execute(
                    "UPDATE users SET updated_at = datetime('now'), photo = ?, name = ? WHERE id = ?",
                    (photo, name, existing['id'])
                )
                conn.commit()
                user_db = dict(existing)
                
                # Синхронизация с облаком
                cloud_user = cloud_get_user(user_id)
                if cloud_user and cloud_user.get("credits", 0) > user_db.get("credits", 0):
                    user_db["credits"] = cloud_user["credits"]
                    cursor.execute("UPDATE users SET credits = ? WHERE id = ?", 
                                 (cloud_user["credits"], existing['id']))
                    conn.commit()
            else:
                cloud_user = cloud_get_user(user_id)
                if cloud_user:
                    cursor.execute(
                        "INSERT OR REPLACE INTO users (id, vk_id, name, photo, referral_code, balance, credits) VALUES (?, ?, ?, ?, ?, ?, ?)",
                        (cloud_user['id'], cloud_user['vk_id'], cloud_user['name'], photo,
                         cloud_user['referral_code'], cloud_user.get('balance', 0), cloud_user.get('credits', 3))
                    )
                    conn.commit()
                    user_db = cloud_user
                    user_db['photo'] = photo
                else:
                    referral_code = ''.join(secrets.choice(string.ascii_letters + string.digits) for _ in range(8))
                    cursor.execute(
                        "INSERT INTO users (id, vk_id, name, photo, referral_code, balance, credits) VALUES (?, ?, ?, ?, ?, 0, 3)",
                        (user_uuid, user_id, name, photo, referral_code)
                    )
                    conn.commit()
                    cursor.execute("SELECT * FROM users WHERE id = ?", (user_uuid,))
                    user_db = dict(cursor.fetchone())
                    cloud_save_user(user_db)

        # Редирект на фронтенд
        user_json = json.dumps(user_db, default=str)
        user_json_encoded = urllib.parse.quote(user_json)
        redirect_url = f"{FRONTEND_URL}?auth=success&userData={user_json_encoded}"

        return HTMLResponse(f"""
            <html>
            <head><meta http-equiv="refresh" content="0;url={redirect_url}"></head>
            <body><p>✅ Авторизация успешна! Перенаправляем...</p>
            <script>window.location.href = '{redirect_url}';</script></body>
            </html>
        """)

    except Exception as e:
        print(f"Auth error: {e}")
        return HTMLResponse(f"<h1>Ошибка авторизации</h1><p>{str(e)}</p>")


@app.post("/chat/send")
async def send_message(message: dict, request: Request):
    user_id = request.headers.get("X-User-ID")
    prompt = message.get("prompt", "").strip()
    msg_type = message.get("type", "text")

    if not user_id:
        raise HTTPException(status_code=401, detail="Unauthorized")
    if not prompt:
        raise HTTPException(status_code=400, detail="Empty prompt")

    # Получаем ответ от GigaChat
    ai_response = ask_gigachat(prompt, msg_type)
    
    if not ai_response:
        ai_response = "🤖 Нейросети загружаются. Попробуйте через 30 секунд."

    # Сохраняем в историю
    msg_id_1 = str(uuid.uuid4())
    msg_id_2 = str(uuid.uuid4())
    
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO chat_history (id, user_id, role, content, message_type) VALUES (?, ?, 'user', ?, ?)",
            (msg_id_1, user_id, prompt, msg_type)
        )
        cursor.execute(
            "INSERT INTO chat_history (id, user_id, role, content, message_type) VALUES (?, ?, 'assistant', ?, ?)",
            (msg_id_2, user_id, ai_response, "text")
        )
        conn.commit()

    return {"response": ai_response, "type": "text"}


@app.get("/chat/history/{user_id}")
async def get_history(user_id: str):
    with get_db() as conn:
        cursor = conn.cursor()
        history = cursor.execute(
            "SELECT * FROM chat_history WHERE user_id = ? ORDER BY created_at DESC LIMIT 50",
            (user_id,)
        ).fetchall()
    return {"history": [dict(msg) for msg in history]}


@app.post("/credits/deduct")
async def deduct_credits(data: dict, request: Request):
    user_id = request.headers.get("X-User-ID")
    amount = float(data.get("amount", 1))

    with get_db() as conn:
        cursor = conn.cursor()
        result = cursor.execute(
            "SELECT credits, vk_id FROM users WHERE id = ?", (user_id,)
        ).fetchone()

        if result and float(result[0]) >= amount:
            new_credits = float(result[0]) - amount
            cursor.execute(
                "UPDATE users SET credits = ?, updated_at = datetime('now') WHERE id = ?",
                (new_credits, user_id)
            )
            conn.commit()
            cloud_update_credits(result[1], new_credits)
            return {"success": True, "credits": new_credits}

    return {"success": False, "error": "Недостаточно кредитов"}


@app.post("/credits/add")
async def add_credits(data: dict, request: Request):
    user_id = request.headers.get("X-User-ID")
    amount = int(data.get("amount", 0))

    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE users SET credits = credits + ?, updated_at = datetime('now') WHERE id = ?",
            (amount, user_id)
        )
        conn.commit()

        result = cursor.execute(
            "SELECT credits, vk_id FROM users WHERE id = ?", (user_id,)
        ).fetchone()
        if result:
            cloud_update_credits(result[1], result[0])
            return {"success": True, "credits": result[0]}

    return {"success": False, "error": "User not found"}


@app.get("/credits/check")
async def check_credits(request: Request):
    user_id = request.query_params.get("user_id", "")
    if not user_id:
        return {"success": False}

    with get_db() as conn:
        cursor = conn.cursor()
        result = cursor.execute(
            "SELECT credits, balance FROM users WHERE id = ?", (user_id,)
        ).fetchone()
        if result:
            return {"success": True, "credits": result[0], "balance": result[1]}
    return {"success": False}


# ============ STARTUP ============

@app.on_event("startup")
async def startup_event():
    init_db()
    print("=" * 60)
    print("🚀 AI Assistant Pro Backend Started!")
    print(f"📍 Backend URL: {BACKEND_URL}")
    print(f"🌐 Frontend URL: {FRONTEND_URL}")
    print(f"☁️  Cloud: {'Configured' if JSONBIN_KEY else 'Not configured'}")
    print(f"🤖 GigaChat: {'Configured' if GIGACHAT_AUTH_KEY else 'Not configured'}")
    print("=" * 60)


if __name__ == "__main__":
    port = int(os.getenv("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)