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

@app.get("/", response_class=HTMLResponse)
async def homepage():
    return """<html><body style="font-family:Arial;text-align:center;padding:50px;">
    <h1>🤖 Backend работает!</h1><p>База данных SQLite готова.</p></body></html>"""

@app.get("/health")
async def health():
    return {"status": "ok", "database": "sqlite"}

# ============================================
# АВТОРИЗАЦИЯ ВКОНТАКТЕ (ИСПРАВЛЕНА!)
# ============================================

@app.get("/auth/vk/login")
async def vk_login_url():
    client_id = os.getenv("VK_CLIENT_ID", "54562656")
    callback = os.getenv("VK_CALLBACK_URL", "https://neuro-guru-backend.onrender.com/auth/vk/callback")
    
    # БЕЗ SCOPE — мини-приложения не поддерживают его
    login_url = f"https://oauth.vk.com/authorize?client_id={client_id}&redirect_uri={callback}&display=page&response_type=code"
    
    return {"login_url": login_url}

@app.get("/auth/vk/callback")
async def vk_callback(code: str):
    client_id = os.getenv("VK_CLIENT_ID", "54562656")
    client_secret = os.getenv("VK_CLIENT_SECRET", "TO2ZBwRkucVuTugyW2z8")
    callback = os.getenv("VK_CALLBACK_URL", "https://neuro-guru-backend.onrender.com/auth/vk/callback")
    
    try:
        # Обмен кода на токен
        token_response = requests.post(
            "https://oauth.vk.com/access_token",
            data={
                "client_id": client_id,
                "client_secret": client_secret,
                "redirect_uri": callback,
                "code": code
            },
            timeout=10
        )
        token_data = token_response.json()
        
        if "access_token" not in token_data:
            raise HTTPException(status_code=400, detail=f"VK Token Error: {token_data}")
        
        access_token = token_data["access_token"]
        user_id = int(token_data["user_id"])
        
        # Получение данных пользователя через API VK
        user_response = requests.get(
            "https://api.vk.com/method/users.get",
            params={
                "access_token": access_token,
                "fields": "first_name,last_name,photo_200",
                "v": "5.131"
            },
            timeout=10
        )
        user_data = user_response.json()
        
        if "response" in user_data and len(user_data["response"]) > 0:
            user_info = user_data["response"][0]
            first_name = user_info.get("first_name", "")
            last_name = user_info.get("last_name", "")
            name = f"{first_name} {last_name}".strip() or "User"
            photo = user_info.get("photo_200", "")
        else:
            name = "User"
            photo = ""
        
        user_uuid = str(secrets.token_hex(16))
        
        with get_db() as conn:
            cursor = conn.cursor()
            existing = cursor.execute("SELECT * FROM users WHERE vk_id = ?", (user_id,)).fetchone()
            
            if existing:
                cursor.execute("UPDATE users SET updated_at = datetime('now'), photo = ? WHERE id = ?", (photo, existing['id']))
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
        
        # Перенаправляем пользователя на фронтенд с данными
        frontend_url = os.getenv("FRONTEND_URL", "https://ai-app-vk.vercel.app")
        
        import json
        import base64
        user_json = json.dumps(user_db, default=str)
        user_b64 = base64.urlsafe_b64encode(user_json.encode()).decode()
        
        redirect_url = f"{frontend_url}?token={session_token}&user={user_b64}"
        
        return HTMLResponse(content=f"""
            <html>
            <head>
                <script>
                    localStorage.setItem('session_token', '{session_token}');
                    localStorage.setItem('current_user', '{user_json}');
                    window.location.href = '{frontend_url}';
                </script>
            </head>
            <body>
                <p>Авторизация успешна! Перенаправляем...</p>
            </body>
            </html>
        """)
        
    except Exception as e:
        print(f"Auth error: {e}")
        raise HTTPException(status_code=500, detail=str(e))

# ============================================
# ЧАТ С ИИ
# ============================================

@app.post("/chat/send")
async def send_message(message: dict, request: Request):
    user_id = request.headers.get("X-User-ID")
    prompt = message.get("prompt", "").strip()
    msg_type = message.get("type", "text")
    
    if not user_id:
        raise HTTPException(status_code=401, detail="Unauthorized")
    if not prompt:
        raise HTTPException(status_code=400, detail="Empty prompt")
    
    # Ответ в зависимости от типа запроса
    if msg_type == "text":
        ai_response = f"Ответ на ваш запрос: '{prompt}'. Подключите YandexGPT API для полноценных ответов."
    elif msg_type == "code":
        ai_response = f"# Код по запросу: {prompt}\nprint('Hello World')\n# Подключите YandexGPT API для генерации реального кода."
    elif msg_type == "image":
        ai_response = f"Изображение по запросу '{prompt}' будет доступно после подключения Stability AI API."
    elif msg_type == "video":
        ai_response = f"Видео по запросу '{prompt}' будет доступно после подключения Replicate API."
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
        history = cursor.execute(
            "SELECT * FROM chat_history WHERE user_id = ? ORDER BY created_at DESC LIMIT 50",
            (user_id,)
        ).fetchall()
    return {"history": [dict(msg) for msg in history]}

# ============================================
# КРЕДИТЫ
# ============================================

@app.post("/credits/deduct")
async def deduct_credits(data: dict, request: Request):
    user_id = request.headers.get("X-User-ID")
    amount = float(data.get("amount", 1))
    
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT credits FROM users WHERE id = ?", (user_id,))
        result = cursor.fetchone()
        
        if result and float(result[0]) >= amount:
            cursor.execute(
                "UPDATE users SET credits = credits - ?, updated_at = datetime('now') WHERE id = ?",
                (amount, user_id)
            )
            conn.commit()
            return {"success": True, "deducted": amount}
    
    return {"success": False, "error": "Недостаточно кредитов"}

# ============================================
# ЗАПУСК
# ============================================

if __name__ == "__main__":
    print("=" * 60)
    print("🚀 ЗАПУСК СЕРВЕРА AI ASSISTANT PRO...")
    print("=" * 60)
    create_tables()
    try:
        uvicorn.run(app, host="0.0.0.0", port=8000, log_level="info")
    except Exception as e:
        print(f"❌ КРИТИЧЕСКАЯ ОШИБКА: {e}")
        import traceback
        traceback.print_exc()
        input("\nНажми Enter для выхода...")