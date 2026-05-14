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
import urllib.parse

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
    code_verifier = secrets.token_urlsafe(64)
    code_challenge = base64.urlsafe_b64encode(
        hashlib.sha256(code_verifier.encode('ascii')).digest()
    ).decode('ascii').rstrip('=')
    return code_verifier, code_challenge

# ============================================
# ГЛАВНАЯ + VK CALLBACK
# ============================================

@app.get("/", response_class=HTMLResponse)
async def homepage(request: Request):
    code = request.query_params.get("code")
    device_id = request.query_params.get("device_id", "")
    state = request.query_params.get("state", "")
    
    if code:
        return await process_vk_auth(code, device_id, state)
    
    return """<html><body style="font-family:Arial;text-align:center;padding:50px;">
    <h1>🤖 Backend работает!</h1><p>VK ID + PKCE готово.</p></body></html>"""

@app.get("/health")
async def health():
    return {"status": "ok", "database": "sqlite", "auth": "vk_id_pkce"}

# ============================================
# АВТОРИЗАЦИЯ VK ID
# ============================================

@app.get("/auth/vk/login")
async def vk_login_url():
    client_id = os.getenv("VK_CLIENT_ID", "54571690")
    callback = BACKEND_URL
    
    code_verifier, code_challenge = generate_pkce()
    state = secrets.token_urlsafe(32)
    pkce_store[state] = code_verifier
    
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
    client_id = os.getenv("VK_CLIENT_ID", "54571690")
    client_secret = os.getenv("VK_CLIENT_SECRET", "AAHXNzlDsumtOLOfMnXt")
    callback = BACKEND_URL
    
    code_verifier = pkce_store.pop(state, secrets.token_urlsafe(64))
    
    try:
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
        user_json_encoded = urllib.parse.quote(user_json)
        
        redirect_url = f"{FRONTEND_URL}?auth=success&token={session_token}&userData={user_json_encoded}"
        
        return HTMLResponse(content=f"""
            <html>
            <head>
                <meta http-equiv="refresh" content="0;url={redirect_url}">
            </head>
            <body style="font-family:Arial;text-align:center;padding:50px;">
                <h1>✅ Авторизация успешна!</h1>
                <p>Перенаправляем...</p>
                <script>window.location.href = '{redirect_url}';</script>
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
    
    hf_token = os.getenv("HF_TOKEN", "")
    ai_response = ""
    
        if msg_type in ["text", "code"] and hf_token:
        try:
            if msg_type == "code":
                formatted_prompt = f"Write code for: {prompt}. Provide only the code."
            else:
                formatted_prompt = prompt
            
            # Пробуем несколько моделей по очереди
            models = [
                "Qwen/Qwen2-1.5B-Instruct",
                "HuggingFaceH4/zephyr-7b-beta",
                "google/flan-t5-base",
                "microsoft/DialoGPT-large"
            ]
            
            ai_response = ""
            
            for model in models:
                try:
                    hf_response = requests.post(
                        f"https://api-inference.huggingface.co/models/{model}",
                        headers={"Authorization": f"Bearer {hf_token}"},
                        json={
                            "inputs": formatted_prompt,
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
                            ai_response = result[0].get("generated_text", "")
                        elif isinstance(result, dict):
                            ai_response = result.get("generated_text", str(result))
                        else:
                            ai_response = str(result)
                        
                        if ai_response:
                            break
                    elif hf_response.status_code == 503:
                        ai_response = f"⏳ Модель {model} загружается... Попробуйте через 30 секунд."
                        continue
                    else:
                        continue
                        
                except Exception as model_error:
                    print(f"Model {model} error: {model_error}")
                    continue
            
            if not ai_response:
                ai_response = "Все модели сейчас загружаются. Попробуйте через 30-60 секунд."
                
        except Exception as e:
            print(f"HF Error: {e}")
            ai_response = f"Ошибка генерации: {str(e)}"
    
    elif msg_type == "image" and hf_token:
        try:
            hf_response = requests.post(
                "https://api-inference.huggingface.co/models/stabilityai/stable-diffusion-xl-base-1.0",
                headers={"Authorization": f"Bearer {hf_token}"},
                json={"inputs": prompt},
                timeout=60
            )
            
            if hf_response.status_code == 200:
                ai_response = "Изображение сгенерировано! (Для полного отображения нужно настроить хранилище)"
            else:
                ai_response = f"Модель загружается. Попробуйте через 30 секунд. (Код: {hf_response.status_code})"
                
        except Exception as e:
            ai_response = f"Ошибка: {str(e)}"
    
    elif msg_type == "video" and hf_token:
        try:
            hf_video_response = requests.post(
                "https://api-inference.huggingface.co/models/ali-vilab/text-to-video-ms-1.7b",
                headers={"Authorization": f"Bearer {hf_token}"},
                json={
                    "inputs": prompt,
                    "parameters": {"num_frames": 16, "num_inference_steps": 25}
                },
                timeout=120
            )
            
            if hf_video_response.status_code == 200:
                ai_response = f"Видео сгенерировано по запросу: '{prompt}'."
            elif hf_video_response.status_code == 503:
                ai_response = "⏳ Модель загружается... Попробуйте через 30-60 секунд."
            else:
                ai_response = f"Модель загружается. Попробуйте через минуту. (Код: {hf_video_response.status_code})"
                
        except requests.exceptions.Timeout:
            ai_response = "⏳ Генерация видео занимает до 2 минут. Попробуйте повторить."
        except Exception as e:
            ai_response = f"Ошибка генерации видео: {str(e)}"
    
    else:
        if msg_type == "text":
            ai_response = f"Ответ на: '{prompt}'. Для полноценной работы подключите API."
        elif msg_type == "code":
            ai_response = f"# Код: {prompt}\nprint('Hello World')\n# Подключите API."
        elif msg_type == "image":
            ai_response = "Подключите Hugging Face API для генерации изображений."
        elif msg_type == "video":
            ai_response = "Подключите Hugging Face API для генерации видео."
        else:
            ai_response = "Неизвестный тип запроса."
    
    msg_id_1 = str(uuid.uuid4())
    msg_id_2 = str(uuid.uuid4())
    
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("INSERT INTO chat_history (id, user_id, role, content, message_type) VALUES (?, ?, 'user', ?, ?)", (msg_id_1, user_id, prompt, msg_type))
        cursor.execute("INSERT INTO chat_history (id, user_id, role, content, message_type) VALUES (?, ?, 'assistant', ?, ?)", (msg_id_2, user_id, ai_response, msg_type))
        conn.commit()
    
    return {"response": ai_response, "type": msg_type}

@app.get("/chat/history/{user_id}")
async def get_history(user_id: str):
    with get_db() as conn:
        cursor = conn.cursor()
        history = cursor.execute("SELECT * FROM chat_history WHERE user_id = ? ORDER BY created_at DESC LIMIT 50", (user_id,)).fetchall()
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
            cursor.execute("UPDATE users SET credits = credits - ?, updated_at = datetime('now') WHERE id = ?", (amount, user_id))
            conn.commit()
            return {"success": True, "deducted": amount}
    
    return {"success": False, "error": "Недостаточно кредитов"}

# ============================================
# ПЛАТЕЖИ ЮKASSA
# ============================================

@app.post("/payment/create")
async def create_payment(payment_data: dict, request: Request):
    user_id = request.headers.get("X-User-ID")
    package = payment_data.get("package")
    amount = float(payment_data.get("amount", 0))
    
    shop_id = os.getenv("YOOKASSA_SHOP_ID", "")
    secret_key = os.getenv("YOOKASSA_SECRET_KEY", "")
    
    if not shop_id or not secret_key:
        return {"success": False, "error": "ЮKassa не настроена"}
    
    packages = {
        'starter': {'credits': 150, 'desc': 'Пакет Starter — 150 кредитов'},
        'professional': {'credits': 450, 'desc': 'Пакет Professional — 450 кредитов'},
        'business': {'credits': 1100, 'desc': 'Пакет Business — 1100 кредитов'},
        'unlimited': {'credits': 3500, 'desc': 'Пакет Unlimited — 3500 кредитов'}
    }
    
    pkg = packages.get(package)
    if not pkg:
        return {"success": False, "error": "Неизвестный пакет"}
    
    try:
        from yookassa import Configuration, Payment as YooPayment
        Configuration.account_id = shop_id
        Configuration.secret_key = secret_key
        
        payment = YooPayment.create({
            "amount": {"value": f"{amount:.2f}", "currency": "RUB"},
            "confirmation": {"type": "redirect", "return_url": f"{FRONTEND_URL}?payment=success&user={user_id}&pkg={package}"},
            "capture": True,
            "description": pkg['desc'],
            "metadata": {"user_id": user_id, "package": package, "credits": pkg['credits']}
        }, str(uuid.uuid4()))
        
        return {"success": True, "payment_id": payment.id, "confirmation_url": payment.confirmation.confirmation_url}
        
    except Exception as e:
        print(f"Payment error: {e}")
        return {"success": False, "error": str(e)}

@app.post("/payment/webhook")
async def payment_webhook(request: Request):
    try:
        body = await request.body()
        data = json.loads(body)
        
        if data.get("event") == "payment.succeeded":
            metadata = data.get("object", {}).get("metadata", {})
            user_id = metadata.get("user_id")
            credits_to_add = int(metadata.get("credits", 0))
            
            if user_id and credits_to_add > 0:
                with get_db() as conn:
                    cursor = conn.cursor()
                    cursor.execute("UPDATE users SET credits = credits + ?, updated_at = datetime('now') WHERE id = ?", (credits_to_add, user_id))
                    conn.commit()
                print(f"✅ +{credits_to_add} кредитов для {user_id}")
        
        return {"status": "ok"}
    except Exception as e:
        print(f"Webhook error: {e}")
        return {"status": "error"}

# ============================================
# ЗАПУСК
# ============================================

if __name__ == "__main__":
    print("=" * 60)
    print("🚀 ЗАПУСК AI ASSISTANT PRO")
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