import os
import io
import base64
import sqlite3
import random
import string
from flask import Flask, render_template, request, jsonify, redirect, make_response
from google import genai
from google.genai import types
from PIL import Image
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__)

API_KEY = os.environ.get("GEMINI_API_KEY")
client = genai.Client(api_key=API_KEY)
DB_FILE = "chat.db"

def init_db():
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute('CREATE TABLE IF NOT EXISTS users (token TEXT PRIMARY KEY, username TEXT NOT NULL)')
    cursor.execute('CREATE TABLE IF NOT EXISTS threads (id INTEGER PRIMARY KEY, user_token TEXT, title TEXT DEFAULT "New Chat")')
    cursor.execute('CREATE TABLE IF NOT EXISTS messages (id INTEGER PRIMARY KEY AUTOINCREMENT, thread_id INTEGER, sender TEXT, text TEXT, image_b64 TEXT)')
    conn.commit()
    conn.close()

init_db()

def get_current_user():
    token = request.cookies.get("auth_token")
    if not token: return None
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("SELECT token, username FROM users WHERE token = ?", (token,))
    user = cursor.fetchone()
    conn.close()
    return {"token": user[0], "username": user[1]} if user else None

@app.route('/login', methods=['GET', 'POST'])
def login_route():
    error_msg = None
    if request.method == 'POST':
        action = request.form.get("action")
        username = request.form.get("username", "").strip()
        if action == "register" and username:
            new_token = ''.join(random.choices(string.ascii_uppercase + string.digits, k=12))
            conn = sqlite3.connect(DB_FILE)
            cursor = conn.cursor()
            cursor.execute("INSERT INTO users (token, username) VALUES (?, ?)", (new_token, username))
            conn.commit()
            conn.close()
            response = make_response(redirect('/'))
            response.set_cookie("auth_token", new_token, max_age=60*60*24*365)
            return response
        elif action == "login":
            token = request.form.get("token", "").strip()
            conn = sqlite3.connect(DB_FILE)
            cursor = conn.cursor()
            cursor.execute("SELECT username FROM users WHERE token = ?", (token,))
            row = cursor.fetchone()
            conn.close()
            if row and row[0].lower() == username.lower():
                response = make_response(redirect('/'))
                response.set_cookie("auth_token", token, max_age=60*60*24*365)
                return response
            error_msg = "Validation Error: Credentials mismatch."
    return render_template('index.html', is_login_route=True, error=error_msg)

@app.route('/')
def root_route():
    user = get_current_user()
    if not user: return redirect('/login')
    return redirect('/chat/0')

@app.route('/logout')
def logout():
    response = make_response(redirect('/login'))
    response.delete_cookie("auth_token")
    return response

@app.route('/chat/<int:thread_id>')
def chat_room(thread_id):
    user = get_current_user()
    if not user: return redirect('/login')
    
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    
    if thread_id != 0:
        cursor.execute("SELECT user_token FROM threads WHERE id = ?", (thread_id,))
        row = cursor.fetchone()
        if not row or row[0] != user["token"]:
            conn.close()
            return "Access Violation Error", 403
            
    cursor.execute('''
        SELECT DISTINCT t.id, t.title FROM threads t 
        INNER JOIN messages m ON t.id = m.thread_id 
        WHERE t.user_token = ? ORDER BY t.id DESC LIMIT 40
    ''', (user["token"],))
    all_threads = [{"id": r[0], "title": r[1]} for r in cursor.fetchall()]
    conn.close()
    
    return render_template('index.html', is_login_route=False, current_id=thread_id, threads=all_threads, user=user)

@app.route('/api/history/<int:thread_id>', methods=['GET'])
def get_history(thread_id):
    if thread_id == 0: return jsonify({"history": []})
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("SELECT id, sender, text, image_b64 FROM messages WHERE thread_id = ? ORDER BY id ASC", (thread_id,))
    rows = cursor.fetchall()
    conn.close()
    return jsonify({"history": [{"id": r[0], "sender": r[1], "text": r[2], "image": r[3]} for r in rows]})

@app.route('/api/chat/<int:thread_id>', methods=['POST'])
def handle_chat_message(thread_id):
    user = get_current_user()
    if not user: return jsonify({"error": "Session expired"}), 401
    data = request.json or {}
    user_message = data.get("message", "").strip()
    image_b64 = data.get("image", None)
    
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    
    if thread_id == 0:
        thread_id = random.randint(100000, 999999)
        cursor.execute("INSERT INTO threads (id, user_token, title) VALUES (?, ?, ?)", (thread_id, user["token"], "New Chat"))
        conn.commit()
        
    cursor.execute("INSERT INTO messages (thread_id, sender, text, image_b64) VALUES (?, ?, ?, ?)", (thread_id, "user", user_message, image_b64))
    conn.commit()
    
    cursor.execute("SELECT sender, text, image_b64 FROM messages WHERE thread_id = ? ORDER BY id ASC", (thread_id,))
    full_logs = cursor.fetchall()
    
    contents = []
    system_instruction = f"Your name is Mohammad AI. You are talking directly with {user['username']}."
    
    for sender, text, img in full_logs:
        parts = []
        if text: parts.append(types.Part.from_text(text=text))
        if img:
            try:
                # FIXED: Strip metadata header correctly to prevent Pillow crash errors
                encoded = img.split(",", 1)[1] if "," in img else img
                parts.append(Image.open(io.BytesIO(base64.b64decode(encoded))))
            except Exception as e:
                print(f"Image processing error dropped: {e}")
        if parts: contents.append(types.Content(role="user" if sender == "user" else "model", parts=parts))

    try:
        config = types.GenerateContentConfig(system_instruction=system_instruction)
        response = client.models.generate_content(model='gemini-2.5-flash', contents=contents, config=config)
        ai_response_text = response.text
        
        cursor.execute("INSERT INTO messages (thread_id, sender, text, image_b64) VALUES (?, ?, ?, ?)", (thread_id, "model", ai_response_text, None))
        
        cursor.execute("SELECT COUNT(*) FROM messages WHERE thread_id = ?", (thread_id,))
        if cursor.fetchone()[0] <= 2 and user_message:
            summary_prompt = f"Short brief title (max 3 words) summarizing: {user_message}"
            title_resp = client.models.generate_content(model='gemini-2.5-flash', contents=[summary_prompt])
            cursor.execute("UPDATE threads SET title = ? WHERE id = ?", (title_resp.text.strip(), thread_id))
            
        conn.commit()
        conn.close()
        return jsonify({"response": ai_response_text, "new_thread_id": thread_id})
    except Exception as e:
        conn.close()
        return jsonify({"error": str(e)}), 500

@app.route('/api/edit-message/<int:msg_id>', methods=['POST'])
def edit_message(msg_id):
    user = get_current_user()
    if not user: return jsonify({"error": "Unauthorized"}), 401
    data = request.json or {}
    new_text = data.get("text", "").strip()
    
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("UPDATE messages SET text = ? WHERE id = ?", (new_text, msg_id))
    conn.commit()
    conn.close()
    return jsonify({"status": "success"})

@app.route('/api/delete-thread/<int:thread_id>', methods=['POST'])
def delete_thread(thread_id):
    user = get_current_user()
    if not user: return jsonify({"error": "Unauthorized"}), 401
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("DELETE FROM threads WHERE id = ? AND user_token = ?", (thread_id, user["token"]))
    cursor.execute("DELETE FROM messages WHERE thread_id = ?", (thread_id,))
    conn.commit()
    conn.close()
    return jsonify({"status": "success"})

@app.route('/api/generate-image', methods=['POST'])
def make_image_endpoint():
    data = request.json or {}
    prompt = data.get("prompt", "").strip()
    try:
        response = client.models.generate_content(
            model="gemini-2.5-flash-image", contents=[prompt],
            config=types.GenerateContentConfig(response_modalities=["IMAGE"], image_config=types.ImageConfig(aspect_ratio="1:1"))
        )
        for part in response.parts:
            if part.inline_data:
                return jsonify({"image": f"data:image/png;base64,{base64.b64encode(part.inline_data.data).decode('utf-8')}"})
    except Exception as e:
        # FIXED: Handles Free-tier resource exhausted bounds safely
        if "429" in str(e) or "RESOURCE_EXHAUSTED" in str(e):
            return jsonify({"error": "Gemini Engine Image Quota Exhausted. Please wait 60 seconds before generating imagery tracks again."}), 429
        return jsonify({"error": str(e)}), 500

if __name__ == '__main__':
    app.run(debug=True, port=5000, host="0.0.0.0")