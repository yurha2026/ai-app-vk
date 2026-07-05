from fastapi import FastAPI, Request, HTTPException, Depends, Header
from fastapi.responses import HTMLResponse
from fastapi.middleware.cors import CORSMiddleware
import os
import sqlite3
import requests
from dotenv import load_dotenv
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
import hmac

load_dotenv()

app = FastAPI(title="AI Assistant Pro", version="2.0.0")

# Настройка CORS (без allow_credentials при *)
FRONTEND_URL = os.getenv("FRONTEND_URL", "https://ai-app-vk.vercel.app")
BACKEND_URL = os.getenv("API_BASE", "https://neuro-guru-backend.onrender.com")

allowed_origins = [
    FRONTEND_URL,
    "https://vk.com",
    "https://id.vk.com",
    "https://neuro-guru-backend.onrender.com",
    "http://localhost:5173",
    "http://localhost:3000",
    "http://127.0.0.1:5173",
    "http://127.0.0.1:3000",
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=allowed_origins,
    allow_credentials=False,
    allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
    allow_headers=["*"],
)

DATABASE = "database.db"
JSONBIN_KEY = os.getenv("JSONBIN_KEY", "")
JSONBIN_ID = os.getenv("JSONBIN_ID", "")

# Хранилища
pkce_store = {}
gigachat_token_cache = {"token": "", "expires": 0}
session_store = {}  # token -> user_id

# ==================== БАЗА ДАННЫХ ====================


@contextmanager
def get_db():
    conn = sqlite3.connect(DATABASE, timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()


def create_tables():
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
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
            """
        )
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS chat_history (
                id TEXT PRIMARY KEY,
                user_id TEXT,
                role TEXT,
                content TEXT,
                message_type TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS referrals (
                id TEXT PRIMARY KEY,
                referrer_id TEXT,
                referee_id TEXT,
                reward_amount REAL DEFAULT 0.0,
                status TEXT DEFAULT 'pending',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        conn.commit()


# ==================== ОБЛАКО (JSONBIN) ====================


def cloud_get_all():
    if not JSONBIN_KEY or not JSONBIN_ID:
        return {"users": {}}
    try:
        r = requests.get(
            f"https://api.jsonbin.io/v3/b/{JSONBIN_ID}/latest",
            headers={"X-Master-Key": JSONBIN_KEY},
            timeout=10,
        )
        if r.status_code == 200:
            data = r.json()
            record = data.get("record")
            if isinstance(record, dict):
                if "users" not in record:
                    record = {"users": record} if record else {"users": {}}
                return record
            return {"users": {}}
    except Exception as e:
        print(f"[CLOUD] Ошибка чтения: {e}")
    return {"users": {}}


def cloud_save_all(data):
    if not JSONBIN_KEY or not JSONBIN_ID:
        return False
    try:
        r = requests.put(
            f"https://api.jsonbin.io/v3/b/{JSONBIN_ID}",
            headers={"X-Master-Key": JSONBIN_KEY, "Content-Type": "application/json"},
            json=data,
            timeout=15,
        )
        return r.status_code == 200
    except Exception as e:
        print(f"[CLOUD] Ошибка записи: {e}")
        return False


def cloud_save_user(user_data):
    try:
        data = cloud_get_all()
        vk_id = str(user_data.get("vk_id", ""))
        if not vk_id:
            return False
        data.setdefault("users", {})[vk_id] = user_data
        return cloud_save_all(data)
    except Exception as e:
        print(f"[CLOUD] Ошибка сохранения пользователя: {e}")
        return False


def cloud_get_user(vk_id):
    try:
        data = cloud_get_all()
        users = data.get("users", {})
        return users.get(str(vk_id))
    except Exception:
        return None


def cloud_update_credits(vk_id, credits):
    try:
        data = cloud_get_all()
        vk_id_str = str(vk_id)
        if vk_id_str in data.get("users", {}):
            data["users"][vk_id_str]["credits"] = int(credits)
            cloud_save_all(data)
            return True
    except Exception as e:
        print(f"[CLOUD] Ошибка обновления кредитов: {e}")
    return False


# ==================== АУТЕНТИФИКАЦИЯ ====================


def generate_pkce():
    code_verifier = secrets.token_urlsafe(64)
    code_challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(code_verifier.encode("ascii")).digest())
        .decode("ascii")
        .rstrip("=")
    )
    return code_verifier, code_challenge


def create_session(user_id: str) -> str:
    token = f"{user_id}_{secrets.token_hex(32)}"
    session_store[token] = user_id
    return token


async def get_current_user(
    authorization: str = Header(default=None),
    x_user_id: str = Header(default=None),
) -> dict:
    """
    Получение текущего пользователя из сессии.
    Приоритет: Authorization Bearer token
    """
    user_id = None

    if authorization and authorization.startswith("Bearer "):
        token = authorization.replace("Bearer ", "").strip()
        user_id = session_store.get(token)

    if not user_id and x_user_id:
        user_id = x_user_id

    if not user_id:
        raise HTTPException(status_code=401, detail="Не авторизован")

    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM users WHERE id = ?", (user_id,))
        row = cursor.fetchone()
        if not row:
            raise HTTPException(status_code=401, detail="Пользователь не найден")

        user = dict(row)
        return user


# ==================== GIGACHAT ====================


def get_gigachat_token():
    now = time.time()
    if gigachat_token_cache["token"] and gigachat_token_cache["expires"] > now:
        return gigachat_token_cache["token"]

    auth_key = os.getenv("GIGACHAT_AUTH_KEY", "").strip()
    if not auth_key:
        return ""

    try:
        r = requests.post(
            "https://ngw.devices.sberbank.ru:9443/api/v2/oauth",
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Accept": "application/json",
                "RqUID": str(uuid.uuid4()),
                "Authorization": f"Basic {auth_key}",
            },
            data={"scope": "GIGACHAT_API_PERS"},
            verify=False,
            timeout=10,
        )
        if r.status_code == 200:
            data = r.json()
            token = data.get("access_token", "")
            if token:
                gigachat_token_cache["token"] = token
                gigachat_token_cache["expires"] = now + 1800
                return token
    except Exception as e:
        print(f"[GIGACHAT] Ошибка получения токена: {e}")
    return ""


def ask_gigachat(prompt: str, msg_type: str = "text") -> str:
    token = get_gigachat_token()
    if not token:
        return ""

    if msg_type == "code":
        system_msg = "Ты опытный программист. Пиши только чистый, рабочий код. Без объяснений, без комментариев, без лишнего текста."
    else:
        system_msg = "Ты полезный ИИ-ассистент. Отвечай подробно, по-русски, чётко и по существу."

    try:
        r = requests.post(
            "https://gigachat.devices.sberbank.ru/api/v1/chat/completions",
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json",
                "Authorization": f"Bearer {token}",
            },
            json={
                "model": "GigaChat",
                "messages": [
                    {"role": "system", "content": system_msg},
                    {"role": "user", "content": prompt},
                ],
                "temperature": 0.7,
                "max_tokens": 2048,
            },
            verify=False,
            timeout=45,
        )
        if r.status_code == 200:
            data = r.json()
            choices = data.get("choices", [])
            if choices:
                content = choices[0].get("message", {}).get("content", "")
                return content.strip()
    except Exception as e:
        print(f"[GIGACHAT] Ошибка генерации: {e}")
    return ""


def ask_huggingface(prompt: str, hf_token: str) -> str:
    if not hf_token:
        return ""
    models = [
        "Qwen/Qwen2.5-1.5B-Instruct",
        "HuggingFaceH4/zephyr-7b-beta",
        "microsoft/Phi-3-mini-4k-instruct",
    ]
    for model in models:
        try:
            r = requests.post(
                f"https://api-inference.huggingface.co/models/{model}",
                headers={"Authorization": f"Bearer {hf_token}"},
                json={
                    "inputs": prompt,
                    "parameters": {
                        "max_new_tokens": 512,
                        "temperature": 0.7,
                        "return_full_text": False,
                    },
                },
                timeout=40,
            )
            if r.status_code == 200:
                result = r.json()
                if isinstance(result, list) and result:
                    text = result[0].get("generated_text", "")
                    if text and text.strip():
                        return text.strip()
        except Exception:
            continue
    return ""


# ==================== МАРШРУТЫ ====================


@app.get("/", response_class=HTMLResponse)
async def homepage(request: Request):
    code = request.query_params.get("code")
    device_id = request.query_params.get("device_id", "")
    state = request.query_params.get("state", "")
    if code:
        return await process_vk_auth(code, device_id, state)
    return """
    <html>
        <head>
            <meta charset="utf-8">
            <title>AI Assistant Pro API</title>
            <style>
                body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; text-align: center; padding: 60px; background: linear-gradient(135deg, #667eea 0%, #764ba2 100%); color: white; margin: 0; min-height: 100vh; display: flex; align-items: center; justify-content: center; }
                h1 { font-size: 3em; margin-bottom: 20px; text-shadow: 0 10px 30px rgba(0,0,0,0.3); }
                p { font-size: 1.2em; opacity: 0.95; }
                .status { margin-top: 40px; padding: 20px; background: rgba(255,255,255,0.15); border-radius: 20px; backdrop-filter: blur(10px); }
            </style>
        </head>
        <body>
            <div>
                <h1>AI Assistant Pro</h1>
                <p>Backend работает стабильно</p>
                <div class="status">
                    <p>API готов к работе</p>
                </div>
            </div>
        </body>
    </html>
    """


@app.get("/health")
async def health():
    gc_token = get_gigachat_token()
    gigachat_status = "connected" if gc_token else "not_connected"
    cloud_status = "connected" if (JSONBIN_KEY and JSONBIN_ID) else "not_configured"
    return {
        "status": "ok",
        "version": "2.0.0",
        "database": "sqlite",
        "gigachat": gigachat_status,
        "cloud": cloud_status,
        "sessions": len(session_store),
    }


@app.get("/auth/vk/login")
async def vk_login_url():
    client_id = os.getenv("VK_CLIENT_ID", "54571690")
    callback = BACKEND_URL
    code_verifier, code_challenge = generate_pkce()
    state = secrets.token_urlsafe(32)
    pkce_store[state] = code_verifier
    login_url = (
        f"https://id.vk.com/authorize?response_type=code"
        f"&client_id={client_id}"
        f"&redirect_uri={urllib.parse.quote(callback)}"
        f"&state={state}"
        f"&code_challenge={code_challenge}"
        f"&code_challenge_method=S256"
        f"&scope=vkid.personal_info"
    )
    return {"success": True, "login_url": login_url}


async def process_vk_auth(code: str, device_id: str = "", state: str = ""):
    client_id = os.getenv("VK_CLIENT_ID", "54571690")
    client_secret = os.getenv("VK_CLIENT_SECRET", "AAHXNzlDsumtOLOfMnXt")
    callback = BACKEND_URL

    code_verifier = pkce_store.pop(state, None)
    if not code_verifier:
        code_verifier = secrets.token_urlsafe(64)

    try:
        token_resp = requests.post(
            "https://id.vk.com/oauth2/auth",
            data={
                "grant_type": "authorization_code",
                "client_id": client_id,
                "client_secret": client_secret,
                "redirect_uri": callback,
                "code": code,
                "code_verifier": code_verifier,
                "device_id": device_id,
                "state": state,
            },
            timeout=15,
        )
        token_data = token_resp.json()

        if "access_token" not in token_data:
            error_msg = token_data.get("error_description", json.dumps(token_data))
            html = f"""
            <html><body style='font-family:Arial;padding:40px;text-align:center;'>
            <h1>Ошибка авторизации</h1>
            <p>{error_msg}</p>
            <a href='{FRONTEND_URL}'>Вернуться</a>
            </body></html>
            """
            return HTMLResponse(content=html)

        access_token = token_data["access_token"]
        user_id_vk = int(token_data.get("user_id") or 0)

        user_resp = requests.post(
            "https://id.vk.com/oauth2/user_info",
            data={"access_token": access_token, "client_id": client_id},
            timeout=15,
        )
        user_data = user_resp.json()

        user_info = user_data.get("user", {}) or {}
        first_name = user_info.get("first_name", "")
        last_name = user_info.get("last_name", "")
        name = f"{first_name} {last_name}".strip() or "Пользователь"
        photo = user_info.get("avatar", "")

        if not user_id_vk:
            user_id_vk = int(user_info.get("user_id") or 0)

        user_uuid = None
        user_db = None

        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM users WHERE vk_id = ?", (user_id_vk,))
            existing = cursor.fetchone()

            if existing:
                existing_dict = dict(existing)
                user_uuid = existing_dict["id"]

                cloud_user = cloud_get_user(user_id_vk)
                final_credits = existing_dict.get("credits", 3)
                if cloud_user:
                    cloud_credits = int(cloud_user.get("credits", final_credits))
                    if cloud_credits != final_credits:
                        final_credits = cloud_credits

                cursor.execute(
                    """
                    UPDATE users 
                    SET name = ?, photo = ?, credits = ?, updated_at = datetime('now')
                    WHERE id = ?
                    """,
                    (name, photo, final_credits, user_uuid),
                )
                conn.commit()

                cursor.execute("SELECT * FROM users WHERE id = ?", (user_uuid,))
                row = cursor.fetchone()
                user_db = dict(row)

                cloud_save_user(user_db)

            else:
                cloud_user = cloud_get_user(user_id_vk)
                if cloud_user:
                    user_uuid = cloud_user.get("id") or str(secrets.token_hex(16))
                    cloud_credits = int(cloud_user.get("credits", 3))
                    cloud_balance = float(cloud_user.get("balance", 0.0))
                    referral_code = cloud_user.get("referral_code") or "".join(
                        secrets.choice(string.ascii_letters + string.digits)
                        for _ in range(8)
                    )

                    cursor.execute(
                        """
                        INSERT OR REPLACE INTO users 
                        (id, vk_id, name, photo, referral_code, balance, credits, created_at, updated_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, COALESCE((SELECT created_at FROM users WHERE id = ?), datetime('now')), datetime('now'))
                        """,
                        (
                            user_uuid,
                            user_id_vk,
                            name,
                            photo,
                            referral_code,
                            cloud_balance,
                            cloud_credits,
                            user_uuid,
                        ),
                    )
                    conn.commit()
                else:
                    user_uuid = str(secrets.token_hex(16))
                    referral_code = "".join(
                        secrets.choice(string.ascii_letters + string.digits)
                        for _ in range(8)
                    )
                    cursor.execute(
                        """
                        INSERT INTO users (id, vk_id, name, photo, referral_code, balance, credits)
                        VALUES (?, ?, ?, ?, ?, 0, 3)
                        """,
                        (user_uuid, user_id_vk, name, photo, referral_code),
                    )
                    conn.commit()

                cursor.execute("SELECT * FROM users WHERE id = ?", (user_uuid,))
                row = cursor.fetchone()
                user_db = dict(row)
                cloud_save_user(user_db)

        if not user_db:
            raise Exception("Не удалось создать пользователя")

        session_token = create_session(user_db["id"])

        user_json = json.dumps(user_db, default=str, ensure_ascii=False)
        user_json_encoded = urllib.parse.quote(user_json)
        redirect_url = (
            f"{FRONTEND_URL}?auth=success"
            f"&token={session_token}"
            f"&userData={user_json_encoded}"
        )

        html = f"""
        <html>
            <head>
                <meta charset="utf-8">
                <meta http-equiv='refresh' content='0;url={redirect_url}'>
                <title>Перенаправление...</title>
                <style>
                    body {{ font-family: Arial; text-align: center; padding: 60px; background: #f5f5f5; }}
                    .loader {{ border: 4px solid #f3f3f3; border-top: 4px solid #667eea; border-radius: 50%; width: 50px; height: 50px; animation: spin 1s linear infinite; margin: 40px auto; }}
                    @keyframes spin {{ 0% {{ transform: rotate(0deg); }} 100% {{ transform: rotate(360deg); }} }}
                </style>
            </head>
            <body>
                <h2>Успешная авторизация</h2>
                <p>Перенаправляем в приложение...</p>
                <div class="loader"></div>
                <script>window.location.href = '{redirect_url}';</script>
            </body>
        </html>
        """
        return HTMLResponse(content=html)

    except Exception as e:
        print(f"[AUTH] Ошибка: {e}")
        html = f"""
        <html><body style='font-family:Arial;padding:40px;text-align:center;'>
        <h1>Произошла ошибка</h1>
        <p>{str(e)}</p>
        <a href='{FRONTEND_URL}'>Вернуться на главную</a>
        </body></html>
        """
        return HTMLResponse(content=html)


@app.post("/chat/send")
async def send_message(message: dict, current_user: dict = Depends(get_current_user)):
    prompt = message.get("prompt", "").strip()
    msg_type = message.get("type", "text")

    if not prompt:
        raise HTTPException(status_code=400, detail="Пустой запрос")

    user_id = current_user["id"]
    hf_token = os.getenv("HF_TOKEN", "")

    ai_response = ""
    response_type = msg_type

    if msg_type in ["text", "code"]:
        ai_response = ask_gigachat(prompt, msg_type)
        if not ai_response and hf_token:
            if msg_type == "code":
                ai_response = ask_huggingface(f"Напиши код: {prompt}", hf_token)
            else:
                ai_response = ask_huggingface(prompt, hf_token)
        if not ai_response:
            ai_response = "Модели временно загружаются. Попробуйте повторить запрос через 30 секунд."
        response_type = "text"

    elif msg_type == "image":
        gc_token = get_gigachat_token()
        if gc_token:
            try:
                r = requests.post(
                    "https://gigachat.devices.sberbank.ru/api/v1/chat/completions",
                    headers={
                        "Content-Type": "application/json",
                        "Accept": "application/json",
                        "Authorization": f"Bearer {gc_token}",
                    },
                    json={
                        "model": "GigaChat",
                        "messages": [{"role": "user", "content": f"Создай изображение: {prompt}"}],
                        "temperature": 0.7,
                    },
                    verify=False,
                    timeout=90,
                )
                if r.status_code == 200:
                    data = r.json()
                    choices = data.get("choices", [])
                    if choices:
                        content = choices[0].get("message", {}).get("content", "")
                        import re

                        file_ids = re.findall(r'src="([^"]+)"', content)
                        if file_ids:
                            file_id = file_ids[0]
                            img_r = requests.get(
                                f"https://gigachat.devices.sberbank.ru/api/v1/files/{file_id}/content",
                                headers={"Authorization": f"Bearer {gc_token}"},
                                verify=False,
                                timeout=60,
                            )
                            if img_r.status_code == 200 and len(img_r.content) > 100:
                                img_b64 = base64.b64encode(img_r.content).decode("utf-8")
                                ct = img_r.headers.get("content-type", "image/png")
                                ai_response = f"data:{ct};base64,{img_b64}"
                                response_type = "image"
                            else:
                                ai_response = content
                                response_type = "text"
                        else:
                            ai_response = content
                            response_type = "text"
                    else:
                        ai_response = "Не удалось сгенерировать изображение. Попробуйте другой запрос."
                        response_type = "text"
                else:
                    ai_response = "Сервис генерации изображений временно недоступен."
                    response_type = "text"
            except Exception as e:
                print(f"[IMAGE] Ошибка: {e}")
                ai_response = "Произошла ошибка при генерации изображения."
                response_type = "text"
        else:
            ai_response = "Сервис генерации изображений загружается. Попробуйте через минуту."
            response_type = "text"

    elif msg_type == "video":
        ai_response = (
            "🎬 Генерация видео находится в разработке.\n\n"
            "Эта функция станет доступна в ближайших обновлениях!"
        )
        response_type = "text"

    else:
        ai_response = "Неизвестный тип запроса."
        response_type = "text"

    msg_id_user = str(uuid.uuid4())
    msg_id_assistant = str(uuid.uuid4())

    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            INSERT INTO chat_history (id, user_id, role, content, message_type)
            VALUES (?, ?, 'user', ?, ?)
            """,
            (msg_id_user, user_id, prompt, msg_type),
        )
        cursor.execute(
            """
            INSERT INTO chat_history (id, user_id, role, content, message_type)
            VALUES (?, ?, 'assistant', ?, ?)
            """,
            (msg_id_assistant, user_id, ai_response, response_type),
        )
        conn.commit()

    return {"success": True, "response": ai_response, "type": response_type}


@app.get("/chat/history")
async def get_history(current_user: dict = Depends(get_current_user)):
    user_id = current_user["id"]
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT id, user_id, role, content, message_type, created_at
            FROM chat_history
            WHERE user_id = ?
            ORDER BY created_at ASC
            LIMIT 100
            """,
            (user_id,),
        )
        rows = cursor.fetchall()
        history = [dict(r) for r in rows]
    return {"success": True, "history": history}


@app.post("/credits/deduct")
async def deduct_credits(data: dict, current_user: dict = Depends(get_current_user)):
    amount = int(data.get("amount", 1))
    if amount <= 0:
        amount = 1

    user_id = current_user["id"]
    vk_id = current_user.get("vk_id")

    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT credits FROM users WHERE id = ?", (user_id,))
        row = cursor.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Пользователь не найден")

        current_credits = int(row["credits"])
        if current_credits < amount:
            return {"success": False, "error": "Недостаточно кредитов"}

        new_credits = current_credits - amount
        cursor.execute(
            "UPDATE users SET credits = ?, updated_at = datetime('now') WHERE id = ?",
            (new_credits, user_id),
        )
        conn.commit()

    if vk_id:
        cloud_update_credits(vk_id, new_credits)

    return {"success": True, "deducted": amount, "credits": new_credits}


@app.post("/credits/add")
async def add_credits(data: dict, current_user: dict = Depends(get_current_user)):
    amount = int(data.get("amount", 0))
    if amount <= 0:
        return {"success": False, "error": "Некорректное количество"}

    user_id = current_user["id"]
    vk_id = current_user.get("vk_id")

    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE users SET credits = credits + ?, updated_at = datetime('now') WHERE id = ?",
            (amount, user_id),
        )
        conn.commit()
        cursor.execute("SELECT credits FROM users WHERE id = ?", (user_id,))
        row = cursor.fetchone()
        new_credits = int(row["credits"]) if row else 0

    if vk_id:
        cloud_update_credits(vk_id, new_credits)

    return {"success": True, "added": amount, "credits": new_credits}


@app.get("/credits/check")
async def check_credits(user_id: str = None, current_user: dict = Depends(get_current_user)):
    if not user_id:
        user_id = current_user["id"]

    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT credits, balance, vk_id FROM users WHERE id = ?", (user_id,))
        row = cursor.fetchone()
        if not row:
            return {"success": False, "error": "Не найден"}

        credits = int(row["credits"])
        balance = float(row["balance"])
        vk_id = row["vk_id"]

    cloud_user = cloud_get_user(vk_id) if vk_id else None
    if cloud_user:
        cloud_credits = int(cloud_user.get("credits", credits))
        if cloud_credits != credits:
            credits = cloud_credits
            with get_db() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    "UPDATE users SET credits = ?, updated_at = datetime('now') WHERE id = ?",
                    (credits, user_id),
                )
                conn.commit()

    return {"success": True, "credits": credits, "balance": balance}


@app.post("/payment/create")
async def create_payment(
    payment_data: dict, current_user: dict = Depends(get_current_user)
):
    package = payment_data.get("package")
    amount = float(payment_data.get("amount", 0))

    shop_id = os.getenv("YOOKASSA_SHOP_ID", "").strip()
    secret_key = os.getenv("YOOKASSA_SECRET_KEY", "").strip()

    if not shop_id or not secret_key:
        return {
            "success": False,
            "error": "Платёжная система временно недоступна. Обратитесь к администратору.",
        }

    packages = {
        "starter": {"credits": 150, "desc": "Пакет Starter"},
        "professional": {"credits": 450, "desc": "Пакет Professional"},
        "business": {"credits": 1100, "desc": "Пакет Business"},
        "unlimited": {"credits": 3500, "desc": "Пакет Unlimited"},
    }

    pkg = packages.get(package)
    if not pkg:
        return {"success": False, "error": "Неизвестный пакет"}

    try:
        from yookassa import Configuration, Payment

        Configuration.account_id = shop_id
        Configuration.secret_key = secret_key

        payment = Payment.create(
            {
                "amount": {"value": f"{amount:.2f}", "currency": "RUB"},
                "confirmation": {
                    "type": "redirect",
                    "return_url": f"{FRONTEND_URL}?payment=success&pkg={package}",
                },
                "capture": True,
                "description": pkg["desc"],
                "metadata": {
                    "user_id": current_user["id"],
                    "vk_id": current_user.get("vk_id"),
                    "package": package,
                    "credits": pkg["credits"],
                },
            },
            str(uuid.uuid4()),
        )

        return {
            "success": True,
            "payment_id": payment.id,
            "confirmation_url": payment.confirmation.confirmation_url,
        }
    except Exception as e:
        print(f"[PAYMENT] Ошибка создания: {e}")
        return {"success": False, "error": "Не удалось создать платёж"}


@app.post("/payment/webhook")
async def payment_webhook(request: Request):
    try:
        body = await request.body()
        body_str = body.decode("utf-8")

        webhook_secret = os.getenv("YOOKASSA_WEBHOOK_SECRET", "").strip()
        if webhook_secret:
            signature = request.headers.get("Content-HMAC", "") or request.headers.get(
                "Idempotence-Key", ""
            )
            try:
                expected = hmac.new(
                    webhook_secret.encode("utf-8"), body, hashlib.sha256
                ).hexdigest()
                if signature and not hmac.compare_digest(signature, expected):
                    pass
            except Exception:
                pass

        data = json.loads(body_str)
        event = data.get("event")

        if event == "payment.succeeded":
            obj = data.get("object", {}) or {}
            metadata = obj.get("metadata", {}) or {}
            user_id = metadata.get("user_id")
            vk_id = metadata.get("vk_id")
            credits_to_add = int(metadata.get("credits", 0))

            if credits_to_add > 0 and user_id:
                with get_db() as conn:
                    cursor = conn.cursor()
                    cursor.execute(
                        "UPDATE users SET credits = credits + ?, updated_at = datetime('now') WHERE id = ?",
                        (credits_to_add, user_id),
                    )
                    conn.commit()
                    if vk_id:
                        cursor.execute("SELECT credits FROM users WHERE id = ?", (user_id,))
                        row = cursor.fetchone()
                        if row:
                            cloud_update_credits(vk_id, int(row["credits"]))

        return {"status": "ok"}
    except Exception as e:
        print(f"[WEBHOOK] Ошибка: {e}")
        return {"status": "error"}


@app.get("/user/me")
async def get_me(current_user: dict = Depends(get_current_user)):
    return {"success": True, "user": current_user}


@app.post("/auth/logout")
async def logout(authorization: str = Header(default=None)):
    if authorization and authorization.startswith("Bearer "):
        token = authorization.replace("Bearer ", "").strip()
        if token in session_store:
            del session_store[token]
    return {"success": True}


# ==================== ЗАПУСК ====================


if __name__ == "__main__":
    print("=" * 60)
    print("AI ASSISTANT PRO v2.0")
    print("=" * 60)
    print(f"Frontend: {FRONTEND_URL}")
    print(f"Backend: {BACKEND_URL}")
    print("=" * 60)
    create_tables()
    uvicorn.run(app, host="0.0.0.0", port=8000, log_level="info")