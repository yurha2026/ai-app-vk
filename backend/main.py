from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
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

load_dotenv()

app = FastAPI(title="AI Assistant Pro")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

DATABASE = "database.db"
FRONTEND_URL = os.getenv("FRONTEND_URL", "https://ai-app-vk.vercel.app")
BACKEND_URL = os.getenv("API_BASE", "https://neuro-guru-backend.onrender.com")

# Хранилище code_verifier для PKCE
pkce_store = {}

@contextmanager
def get_db():
    conn = sqlite3.connect(DATABASE)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()

def create_tables():
    conn = sqlite3.connect(DATABASE)
    cursor = conn.cursor()
    
    cursor.execute('''CREATE TABLE IF NOT EXISTS users (
        id TEXT PRIMARY KEY, vk_id INTEGER UNIQUE, email TEXT, name TEXT,
        photo TEXT, balance REAL DEFAULT 0.0, credits INTEGER DEFAULT 3,
        subscription_status TEXT DEFAULT 'free', referral_code TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''')
    
    cursor.execute('''CREATE TABLE IF NOT EXISTS chat_history (
        id TEXT PRIMARY KEY, user_id TEXT, role TEXT,
        content TEXT, message_type TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''')
    
    cursor.execute('''CREATE TABLE IF NOT EXISTS referrals (
        id TEXT PRIMARY KEY, referrer_id TEXT, referee_id TEXT,
        reward_amount REAL DEFAULT 0.0, status TEXT DEFAULT 'pending',
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''')
    
    conn.commit()
    conn.close()

def generate_pkce():
    """Генерация PKCE параметров для VK ID"""
    code_verifier = secrets.token_urlsafe(64)
    code_challenge = base64.urlsafe_b64encode(
        hashlib.sha256(code_verifier.encode('ascii')).digest()
    ).decode('ascii').rstrip('=')
    return code_verifier, code_challenge

@app.get("/", response_class=HTMLResponse)
async def homepage(request: Request):
    code = request.query_params.get("code")
    device_id = request.query_params.get("device_id", "")
    state = request.query_params.get("state", "")
    
    if code:
        return await process_vk_auth(code, device_id, state)
    
    return """<html><body style="font-family:Arial;text-align:center;padding:50px;">
    <h1>🤖 Backend работает!</h1><p>VK ID + PKCE авторизация готова.</p></body></html>"""

@app.get("/health")
async def health():
    return {"status": "ok", "database": "sqlite", "auth": "vk_id_pkce"}

@app.get("/auth/vk/login")
async def vk_login_url():
    client_id = os.getenv("VK_CLIENT_ID", "54571690")
    callback = BACKEND_URL
    
    # Генерация PKCE
    code_verifier, code_challenge = generate_pkce()
    
    # Генерация state для безопасности
    state = secrets.token_urlsafe(32)
    
    # Сохраняем verifier для использования при обмене кода
    pkce_store[state] = code_verifier
    
    # Формируем URL с PKCE параметрами
    login_url = (
        f"https://id.vk.com/authorize?"
        f"response_type=code"
        f"&client_id={client_id}"
        f"&redirect_uri={callback}"
        f"&state={state}"
        f"&code_challenge={code_challenge}"
        f"&code_challenge_method=S256"
        f"&scope=vkid.personal_info"
    )
    
    return {"login_url": login_url}

async def process_vk_auth(code: str, device_id: str = "", state: str = ""):
    """Обработка кода авторизации от VK ID с PKCE"""
    client_id = os.getenv("VK_CLIENT_ID", "54571690")
    client_secret = os.getenv("VK_CLIENT_SECRET", "AAHXNzlDsumtOLOfMnXt")
    callback = BACKEND_URL
    
    # Получаем сохранённый code_verifier
    code_verifier = pkce_store.pop(state, secrets.token_urlsafe(64))
    
    try:
        # Обмен кода на токен через VK ID API
        token_response = requests.post(
            "https://id.vk.com/oauth2/auth",
            data={
                "grant_type": "authorization_code",
                "client_id": client_id,
                "client_secret": client_secret,
                "redirect_uri": callback,
                "code": code,
                "code_verifier": code_verifier,
                "device_id": device_id,
                "state": state
            },
            timeout=10
        )
        token_data = token_response.json()
        
        if "access_token" not in token_data:
            return HTMLResponse(content=f"""
                <html><body style="font-family:Arial;text-align:center;padding:50px;">
                <h1>❌ Ошибка авторизации</h1>
                <p>{json.dumps(token_data, ensure_ascii=False)}</p>
                <a href="{FRONTEND_URL}">Вернуться на сайт</a>
                </body></html>
            """)
        
        access_token = token_data["access_token"]
        user_id = int(token_data.get("user_id", 0))
        
        # Получение данных пользователя через VK ID API
        user_response = requests.post(
            "https://id.vk.com/oauth2/user_info",
            data={"access_token": access_token, "client_id": client_id},
            timeout=10
        )
        user_data = user_response.json()
        
        first_name = user_data.get("user", {}).get("first_name", "")
        last_name = user_data.get("user", {}).get("last_name", "")
        name = f"{first_name} {last_name}".strip() or "User"
        photo = user_data.get("user", {}).get("avatar", "")
        
        if not user_id:
            user_id = int(user_data.get("user", {}).get("user_id", 0))
        
        user_uuid = str(secrets.token_hex(16))
        
        with get_db() as conn:
            cursor = conn.cursor()
            existing = cursor.execute("SELECT * FROM users WHERE vk_id = ?", (user_id,)).fetchone()
            
            if existing:
                cursor.execute(
                    "UPDATE users SET updated_at = datetime('now'), photo = ? WHERE id = ?",
                    (photo, existing['id'])
                )
                conn.commit()
                user_db = dict(existing)
            else:
                referral_code = ''.join(secrets.choice(string.ascii_letters + string.digits) for _ in range(8))
                cursor.execute(
                    "INSERT INTO users (id, vk_id, name, photo, referral_code, balance, credits) VALUES (?, ?, ?, ?, ?, 0, 3)",
                    (user_uuid, user_id, name, photo, referral_code)
                )
                conn.commit()
                cursor.execute("SELECT * FROM users WHERE id = ?", (user_uuid,))
                user_db = dict(cursor.fetchone())
        
        session_token = f"{user_db['id']}_{secrets.token_hex(16)}"
        user_json = json.dumps(user_db, default=str)
        
        return HTMLResponse(content=f"""
            <html>
            <head>
                <script>
                    try {{
                        localStorage.setItem('session_token', '{session_token}');
                        localStorage.setItem('current_user', '{user_json}');
                        window.location.href = '{FRONTEND_URL}';
                    }} catch(e) {{
                        document.body.innerHTML = '<h1>✅ Авторизация успешна!</h1><p>{name}</p><a href="{FRONTEND_URL}">Перейти</a>';
                    }}
                </script>
            </head>
            <body style="font-family:Arial;text-align:center;padding:50px;">
                <h1>✅ Авторизация успешна!</h1>
                <p>Перенаправляем...</p>
            </body>
            </html>
        """)
        
    except Exception as e:
        print(f"Auth error: {e}")
        return HTMLResponse(content=f"""
            <html><body style="font-family:Arial;text-align:center;padding:50px;">
            <h1>❌ Ошибка</h1><p>{str(e)}</p>
            <a href="{FRONTEND_URL}">Вернуться</a>
            </body></html>
        """)

@app.post("/chat/send")
async def send_message(message: dict, request: Request):
    user_id = request.headers.get("X-User-ID")
    prompt = message.get("prompt", "").strip()
    msg_type = message.get("type", "text")
    
    if not user_id:
        raise HTTPException(status_code=401, detail="Unauthorized")
    if not prompt:
        raise HTTPException(status_code=400, detail="Empty prompt")
    
    hf_token = os.getenv("HF_TOKEN", "")
    ai_response = ""
    
    if msg_type in ["text", "code"] and hf_token:
        try:
            # Генерация текста/кода через Hugging Face
            if msg_type == "code":
                formatted_prompt = f"Write code for: {prompt}. Provide only the code without explanations."
            else:
                formatted_prompt = prompt
            
            hf_response = requests.post(
                "https://api-inference.huggingface.co/models/mistralai/Mistral-7B-Instruct-v0.2",
                headers={"Authorization": f"Bearer {hf_token}"},
                json={
                    "inputs": f"[INST] {formatted_prompt} [/INST]",
                    "parameters": {
                        "max_new_tokens": 512,
                        "temperature": 0.7,
                        "return_full_text": False
                    }
                },
                timeout=30
            )
            
            if hf_response.status_code == 200:
                result = hf_response.json()
                if isinstance(result, list) and len(result) > 0:
                    ai_response = result[0].get("generated_text", "Ошибка генерации")
                else:
                    ai_response = str(result)
            else:
                ai_response = f"Ошибка API: {hf_response.status_code}. Модель загружается, попробуйте через 30 секунд."
                
        except Exception as e:
            print(f"HF Error: {e}")
            ai_response = f"Ошибка генерации: {str(e)}"
    
    elif msg_type == "image" and hf_token:
        try:
            # Генерация изображений через Hugging Face
            hf_response = requests.post(
                "https://api-inference.huggingface.co/models/stabilityai/stable-diffusion-xl-base-1.0",
                headers={"Authorization": f"Bearer {hf_token}"},
                json={"inputs": prompt},
                timeout=60
            )
            
            if hf_response.status_code == 200:
                # Сохраняем изображение и возвращаем URL
                ai_response = "Изображение сгенерировано! (Для отображения нужно настроить хранилище файлов)"
            else:
                ai_response = f"Ошибка генерации: {hf_response.status_code}. Модель загружается."
                
        except Exception as e:
            ai_response = f"Ошибка: {str(e)}"
    
    elif msg_type == "video":
        ai_response = "Генерация видео будет доступна в следующем обновлении. Следите за новостями!"
    
    else:
        # Без токена HF — используем заглушку
        if msg_type == "text":
            ai_response = f"Ответ на: '{prompt}'. Для полноценной работы нужен API ключ."
        elif msg_type == "code":
            ai_response = f"# Код: {prompt}\nprint('Hello World')\n# Подключите API."
        elif msg_type == "image":
            ai_response = "Подключите Hugging Face API для генерации изображений."
        else:
            ai_response = "Неизвестный тип запроса."
    
    # Сохранение в историю
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
            (msg_id_2, user_id, ai_response, msg_type)
        )
        conn.commit()
    
    return {"response": ai_response, "type": msg_type}

@app.get("/chat/history/{user_id}")
async def get_history(user_id: str):
    with get_db() as conn:
        cursor = conn.cursor()
        history = cursor.execute("SELECT * FROM chat_history WHERE user_id = ? ORDER BY created_at DESC LIMIT 50", (user_id,)).fetchall()
    return {"history": [dict(msg) for msg in history]}

@app.post("/credits/deduct")
async def deduct_credits(data: dict, request: Request):
    user_id = request.headers.get("X-User-ID")
    amount = float(data.get("amount", 1))
    
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT credits FROM users WHERE id = ?", (user_id,))
        result = cursor.fetchone()
        
        if result and float(result[0]) >= amount:
            cursor.execute("UPDATE users SET credits = credits - ?, updated_at = datetime('now') WHERE id = ?", (amount, user_id))
            conn.commit()
            return {"success": True, "deducted": amount}
    
    return {"success": False, "error": "Недостаточно кредитов"}

if __name__ == "__main__":
    print("=" * 60)
    print("🚀 ЗАПУСК СЕРВЕРА AI ASSISTANT PRO (VK ID + PKCE)")
    print(f"Backend: {BACKEND_URL}")
    print(f"Frontend: {FRONTEND_URL}")
    print("=" * 60)
    create_tables()
    try:
        uvicorn.run(app, host="0.0.0.0", port=8000, log_level="info")
    except Exception as e:
        print(f"❌ ОШИБКА: {e}")
        import traceback
        traceback.print_exc()