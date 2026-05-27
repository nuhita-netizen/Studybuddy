from pathlib import Path
from datetime import datetime
import json
import os
import random
import string
from typing import Any

from dotenv import load_dotenv
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
import mysql.connector
import socketio

try:
    from google import genai
except Exception:
    genai = None


BASE_DIR = Path(__file__).resolve().parent
TEMPLATE_DIR = BASE_DIR / "templates"
STATIC_DIR = BASE_DIR / "static"

load_dotenv(BASE_DIR / ".env")

DB_HOST = os.getenv("DB_HOST", "localhost")
DB_PORT = int(os.getenv("DB_PORT", "3306"))
DB_USER = os.getenv("DB_USER", "root")
DB_PASSWORD = os.getenv("DB_PASSWORD", "")
DB_NAME = os.getenv("DB_NAME", "studybuddy")

gemini_key = os.getenv("GEMINI_KEY")
gemini_model = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")

if genai and gemini_key:
    try:
        ai_client = genai.Client(api_key=gemini_key)
        print("Gemini ready")
    except Exception as exc:
        ai_client = None
        print(f"Gemini setup failed: {exc}")
else:
    ai_client = None
    print("Gemini not configured")


sio = socketio.AsyncServer(
    async_mode="asgi",
    cors_allowed_origins="*",
    max_http_buffer_size=20_000_000,
    logger=False,
    engineio_logger=False,
)

app = FastAPI(title="Study Buddy MySQL")
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
socket_app = socketio.ASGIApp(sio, app)

Row = dict[str, Any]

room_connections: dict[str, dict[str, dict[str, str]]] = {}
sid_index: dict[str, tuple[str, str]] = {}


def connect_server() -> Any:
    return mysql.connector.connect(
        host=DB_HOST,
        port=DB_PORT,
        user=DB_USER,
        password=DB_PASSWORD,
        connection_timeout=5,
    )


def connect_db() -> Any:
    return mysql.connector.connect(
        host=DB_HOST,
        port=DB_PORT,
        user=DB_USER,
        password=DB_PASSWORD,
        database=DB_NAME,
        connection_timeout=5,
    )


def dict_cursor(conn: Any) -> Any:
    return conn.cursor(dictionary=True)


def fetch_one_dict(cur: Any) -> Row | None:
    row = cur.fetchone()
    if row is None:
        return None
    if isinstance(row, dict):
        return row

    column_names = getattr(cur, "column_names", None)
    if column_names:
        return dict(zip(column_names, row))

    raise RuntimeError("Expected a MySQL dictionary cursor row")


def fetch_all_dicts(cur: Any) -> list[Row]:
    rows = cur.fetchall()
    column_names = getattr(cur, "column_names", None)
    result: list[Row] = []

    for row in rows:
        if isinstance(row, dict):
            result.append(row)
        elif column_names:
            result.append(dict(zip(column_names, row)))
        else:
            raise RuntimeError("Expected MySQL dictionary cursor rows")

    return result


def parse_json(value: Any, fallback: Any) -> Any:
    if value is None:
        return fallback
    if isinstance(value, (list, dict)):
        return value
    try:
        return json.loads(value)
    except Exception:
        return fallback


def make_room_code():
    return "".join(random.choices(string.ascii_uppercase + string.digits, k=6))


def clean_user(data):
    name = (data.get("user_name") or data.get("name") or "Student").strip()
    email = (data.get("user_email") or data.get("email") or "").strip().lower()
    subject = (data.get("subject") or "Other").strip()

    if not email:
        safe_name = "".join(ch for ch in name.lower() if ch.isalnum()) or "guest"
        email = f"{safe_name}-{random.randint(1000, 9999)}@guest.local"

    return name, email, subject


def create_database_if_needed():
    conn = connect_server()
    cur = conn.cursor()
    cur.execute(f"CREATE DATABASE IF NOT EXISTS `{DB_NAME}`")
    conn.commit()
    cur.close()
    conn.close()


def init_db():
    create_database_if_needed()
    conn = connect_db()
    cur = conn.cursor()

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS rooms (
            code VARCHAR(10) PRIMARY KEY,
            host VARCHAR(150),
            subject VARCHAR(120),
            members JSON,
            messages JSON,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            active BOOLEAN DEFAULT TRUE
        )
        """
    )

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS waiting_queue (
            id INT AUTO_INCREMENT PRIMARY KEY,
            user_name VARCHAR(100),
            user_email VARCHAR(150),
            subject VARCHAR(120),
            joined_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
        """
    )

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS match_results (
            user_email VARCHAR(150) PRIMARY KEY,
            room_code VARCHAR(10),
            subject VARCHAR(120),
            members JSON,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
        """
    )

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS notes (
            id INT AUTO_INCREMENT PRIMARY KEY,
            room_code VARCHAR(10),
            user_name VARCHAR(100),
            title VARCHAR(255),
            markdown_text TEXT,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
        """
    )

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS study_links (
            id INT AUTO_INCREMENT PRIMARY KEY,
            room_code VARCHAR(10),
            user_name VARCHAR(100),
            title VARCHAR(255),
            url TEXT,
            description TEXT,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
        """
    )

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS documents (
            id INT AUTO_INCREMENT PRIMARY KEY,
            room_code VARCHAR(10),
            user_name VARCHAR(100),
            original_name VARCHAR(255),
            file_path TEXT,
            file_type VARCHAR(50),
            file_size INT,
            uploaded_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
        """
    )

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS user_stats (
            user_email VARCHAR(150) PRIMARY KEY,
            user_name VARCHAR(100),
            total_focus_seconds INT DEFAULT 0,
            current_streak INT DEFAULT 0,
            best_streak INT DEFAULT 0,
            sessions_completed INT DEFAULT 0,
            last_study_date DATE
        )
        """
    )

    conn.commit()
    cur.close()
    conn.close()


def create_unique_room(cur):
    for _ in range(20):
        code = make_room_code()
        cur.execute("SELECT code FROM rooms WHERE code = %s", (code,))
        if not cur.fetchone():
            return code
    raise RuntimeError("Could not create unique room code")


def template(name):
    return FileResponse(TEMPLATE_DIR / name)


def ask_gemini(prompt):
    if not ai_client:
        return None, "AI is not configured. Add GEMINI_KEY to .env."

    try:
        response = ai_client.models.generate_content(
            model=gemini_model,
            contents=prompt,
        )
        return response.text, None
    except Exception as exc:
        return None, str(exc)


try:
    init_db()
    print("MySQL database ready")
except mysql.connector.Error as exc:
    raise SystemExit(
        "MySQL connection failed. Start MySQL in XAMPP/WAMP/MySQL Workbench "
        "or update DB_HOST, DB_PORT, DB_USER, DB_PASSWORD, and DB_NAME in .env. "
        f"Original error: {exc}"
    ) from exc


async def emit_room_users(room_code):
    users = []
    for email, info in room_connections.get(room_code, {}).items():
        users.append({"email": email, "name": info.get("name") or email.split("@")[0]})

    await sio.emit("room-users", {"users": users, "total": len(users)}, room=room_code)


@sio.event
async def connect(sid, environ):
    print(f"Socket connected: {sid}")


@sio.event
async def disconnect(sid):
    old = sid_index.pop(sid, None)
    if not old:
        return

    room_code, email = old
    entry = room_connections.get(room_code, {}).get(email)

    if entry and entry.get("sid") == sid:
        del room_connections[room_code][email]

    if room_code in room_connections and not room_connections[room_code]:
        del room_connections[room_code]

    await emit_room_users(room_code)
    await sio.emit("user-left", {"email": email}, room=room_code)


@sio.event
async def join_room_signal(sid, data):
    room_code = (data.get("room_code") or "").upper().strip()
    user_name = (data.get("user_name") or "Student").strip()
    user_email = (data.get("user_email") or "").lower().strip()

    if not room_code or not user_email:
        return

    await sio.enter_room(sid, room_code)
    room_connections.setdefault(room_code, {})
    room_connections[room_code][user_email] = {"sid": sid, "name": user_name}
    sid_index[sid] = (room_code, user_email)
    await emit_room_users(room_code)


@sio.event
async def leave_room_signal(sid, data):
    room_code = (data.get("room_code") or "").upper().strip()
    user_email = (data.get("user_email") or "").lower().strip()

    if room_code:
        await sio.leave_room(sid, room_code)

    if room_code in room_connections and user_email in room_connections[room_code]:
        del room_connections[room_code][user_email]

    sid_index.pop(sid, None)
    await emit_room_users(room_code)
    await sio.emit("user-left", {"email": user_email}, room=room_code)


@sio.event
async def webrtc_offer(sid, data):
    room_code = (data.get("room_code") or "").upper().strip()
    target = (data.get("target") or "").lower().strip()
    target_info = room_connections.get(room_code, {}).get(target)

    if target_info:
        await sio.emit(
            "webrtc_offer",
            {
                "offer": data.get("offer"),
                "from_name": data.get("from_name"),
                "from_email": data.get("from_email"),
            },
            to=target_info["sid"],
        )


@sio.event
async def webrtc_answer(sid, data):
    room_code = (data.get("room_code") or "").upper().strip()
    target = (data.get("target") or "").lower().strip()
    target_info = room_connections.get(room_code, {}).get(target)

    if target_info:
        await sio.emit(
            "webrtc_answer",
            {
                "answer": data.get("answer"),
                "from_name": data.get("from_name"),
                "from_email": data.get("from_email"),
            },
            to=target_info["sid"],
        )


@sio.event
async def webrtc_ice_candidate(sid, data):
    room_code = (data.get("room_code") or "").upper().strip()
    target = (data.get("target") or "").lower().strip()
    target_info = room_connections.get(room_code, {}).get(target)

    if target_info:
        await sio.emit(
            "webrtc_ice_candidate",
            {
                "candidate": data.get("candidate"),
                "from_name": data.get("from_name"),
                "from_email": data.get("from_email"),
            },
            to=target_info["sid"],
        )


@sio.event
async def chat_message_socket(sid, data):
    room_code = (data.get("room_code") or "").upper().strip()
    if not room_code:
        return

    msg = {
        "user": data.get("user_name") or "Student",
        "text": data.get("text") or "",
        "type": data.get("type") or "message",
        "fileName": data.get("file_name") or "",
        "timestamp": datetime.now().isoformat(),
    }

    conn = connect_db()
    cur = dict_cursor(conn)

    try:
        cur.execute("SELECT messages FROM rooms WHERE code = %s", (room_code,))
        room = fetch_one_dict(cur)

        if room:
            messages = parse_json(room["messages"], [])
            messages.append(msg)
            messages = messages[-200:]
            cur.execute(
                "UPDATE rooms SET messages = %s WHERE code = %s",
                (json.dumps(messages), room_code),
            )
            conn.commit()
    finally:
        cur.close()
        conn.close()

    await sio.emit("new-message", msg, room=room_code)


@app.get("/")
async def home():
    return template("home.html")


@app.get("/login")
async def login():
    return template("login.html")


@app.get("/study")
async def study():
    return template("study.html")


@app.get("/room/{room_code}")
async def room_redirect(room_code: str):
    return template("room.html")


@app.get("/health")
async def health():
    return {"status": "healthy", "ai": ai_client is not None, "database": "mysql"}


@app.get("/ai/ask")
async def ask_ai(question: str, subject: str = "General"):
    prompt = f"""
You are Study Buddy AI, a friendly study tutor.

Subject: {subject}

Student question:
{question}

Answer clearly with:
1. A simple explanation
2. Key points
3. One quick example
4. A short practice question

For math, write formulas in LaTeX and wrap inline math in \\( ... \\).
Example: write \\( \\frac{{1}}{{2}} \\), not raw \\frac{{1}}{{2}}.
"""
    answer, error = ask_gemini(prompt)
    if error:
        return {"success": False, "error": error}
    return {"success": True, "answer": answer}


@app.post("/ai/summarize-room")
async def summarize_room(request: Request):
    data = await request.json()
    room_code = (data.get("room_code") or "").upper().strip()

    if not room_code:
        return {"success": False, "error": "Room code is required"}

    conn = connect_db()
    cur = dict_cursor(conn)

    try:
        cur.execute("SELECT messages, subject FROM rooms WHERE code = %s", (room_code,))
        room = fetch_one_dict(cur)
    finally:
        cur.close()
        conn.close()

    if not room:
        return {"success": False, "error": "Room not found"}

    messages = parse_json(room["messages"], [])
    recent_messages = messages[-50:]
    chat_text = "\n".join(
        f"{msg.get('user', 'Student')}: {msg.get('text', '')}"
        for msg in recent_messages
        if msg.get("type") in ("message", "note")
    )

    if not chat_text.strip():
        return {"success": False, "error": "No chat messages to summarize yet"}

    prompt = f"""
Summarize this study room discussion.

Subject: {room["subject"]}

Chat:
{chat_text}

Give:
1. Main topics discussed
2. Important points
3. Doubts or questions still open
4. Recommended next steps
"""
    answer, error = ask_gemini(prompt)
    if error:
        return {"success": False, "error": error}
    return {"success": True, "summary": answer}


@app.post("/ai/quiz")
async def generate_quiz(request: Request):
    data = await request.json()
    subject = data.get("subject") or "General"
    topic = data.get("topic") or subject
    difficulty = data.get("difficulty") or "medium"

    prompt = f"""
Create a short study quiz.

Subject: {subject}
Topic: {topic}
Difficulty: {difficulty}

Make 5 questions:
- 3 multiple choice questions with 4 options
- 2 short answer questions

After the quiz, include an answer key.
Keep it clear and student-friendly.
"""
    answer, error = ask_gemini(prompt)
    if error:
        return {"success": False, "error": error}
    return {"success": True, "quiz": answer}


@app.post("/ai/study-plan")
async def generate_study_plan(request: Request):
    data = await request.json()
    subject = data.get("subject") or "General"
    goal = data.get("goal") or "revise the topic"
    minutes = int(data.get("minutes") or 45)

    prompt = f"""
Create a focused study plan.

Subject: {subject}
Goal: {goal}
Available time: {minutes} minutes

Give a practical plan with:
1. Warm-up
2. Main study blocks
3. Practice task
4. Final revision
5. Break suggestion
"""
    answer, error = ask_gemini(prompt)
    if error:
        return {"success": False, "error": error}
    return {"success": True, "plan": answer}


@app.post("/verify-student")
async def verify_student(request: Request):
    data = await request.json()
    email = (data.get("email") or "").lower()
    edu_markers = [".edu", ".ac.uk", ".edu.in", "student.", "university.", "college.", ".ac."]
    return {"success": True, "verified": any(marker in email for marker in edu_markers)}


@app.post("/match/join-queue")
async def join_queue(request: Request):
    data = await request.json()
    user_name, user_email, subject = clean_user(data)

    conn = connect_db()
    cur = dict_cursor(conn)

    try:
        cur.execute("SELECT * FROM match_results WHERE user_email = %s", (user_email,))
        existing = fetch_one_dict(cur)

        if existing:
            cur.execute("DELETE FROM match_results WHERE user_email = %s", (user_email,))
            conn.commit()
            return {
                "success": True,
                "matched": True,
                "room_code": existing["room_code"],
                "subject": existing["subject"],
                "members": parse_json(existing["members"], []),
            }

        cur.execute(
            "SELECT code, subject, members FROM rooms WHERE subject = %s AND active = TRUE ORDER BY created_at DESC",
            (subject,),
        )
        open_rooms = fetch_all_dicts(cur)

        for open_room in open_rooms:
            members = parse_json(open_room["members"], [])

            if any(member.get("email") == user_email for member in members):
                conn.commit()
                return {
                    "success": True,
                    "matched": True,
                    "room_code": open_room["code"],
                    "subject": open_room["subject"],
                    "members": members,
                }

            if len(members) == 2:
                members.append({"name": user_name, "email": user_email, "subject": subject})
                cur.execute(
                    "UPDATE rooms SET members = %s WHERE code = %s",
                    (json.dumps(members), open_room["code"]),
                )
                cur.execute("DELETE FROM waiting_queue WHERE user_email = %s", (user_email,))
                conn.commit()
                return {
                    "success": True,
                    "matched": True,
                    "room_code": open_room["code"],
                    "subject": open_room["subject"],
                    "members": members,
                }

        cur.execute("DELETE FROM waiting_queue WHERE user_email = %s", (user_email,))
        cur.execute(
            """
            INSERT INTO waiting_queue (user_name, user_email, subject, joined_at)
            VALUES (%s, %s, %s, %s)
            """,
            (user_name, user_email, subject, datetime.now()),
        )

        cur.execute(
            """
            SELECT user_name, user_email, subject
            FROM waiting_queue
            WHERE subject = %s
            ORDER BY joined_at ASC
            """,
            (subject,),
        )
        waiting = fetch_all_dicts(cur)

        if len(waiting) >= 2:
            group = waiting[:3] if len(waiting) >= 3 else waiting[:2]
            members = [
                {"name": row["user_name"], "email": row["user_email"], "subject": row["subject"]}
                for row in group
            ]
            room_code = create_unique_room(cur)

            cur.execute(
                """
                INSERT INTO rooms (code, host, subject, members, messages, created_at, active)
                VALUES (%s, %s, %s, %s, %s, %s, TRUE)
                """,
                (
                    room_code,
                    members[0]["name"],
                    subject,
                    json.dumps(members),
                    json.dumps([]),
                    datetime.now(),
                ),
            )

            for member in members:
                cur.execute("DELETE FROM waiting_queue WHERE user_email = %s", (member["email"],))
                cur.execute(
                    """
                    REPLACE INTO match_results
                    (user_email, room_code, subject, members, created_at)
                    VALUES (%s, %s, %s, %s, %s)
                    """,
                    (
                        member["email"],
                        room_code,
                        subject,
                        json.dumps(members),
                        datetime.now(),
                    ),
                )

            cur.execute("DELETE FROM match_results WHERE user_email = %s", (user_email,))
            conn.commit()
            return {
                "success": True,
                "matched": True,
                "room_code": room_code,
                "subject": subject,
                "members": members,
            }

        conn.commit()
        return {
            "success": True,
            "matched": False,
            "waiting": len(waiting),
            "message": f"Waiting for another {subject} student... ({len(waiting)}/2)",
        }
    except Exception as exc:
        conn.rollback()
        return {"success": False, "error": str(exc)}
    finally:
        cur.close()
        conn.close()


@app.post("/match/leave-queue")
async def leave_queue(request: Request):
    data = await request.json()
    user_email = (data.get("user_email") or "").lower().strip()

    conn = connect_db()
    cur = conn.cursor()
    cur.execute("DELETE FROM waiting_queue WHERE user_email = %s", (user_email,))
    conn.commit()
    cur.close()
    conn.close()

    return {"success": True}


@app.get("/match/queue-count")
async def queue_count():
    conn = connect_db()
    cur = dict_cursor(conn)
    cur.execute("SELECT COUNT(*) AS count FROM waiting_queue")
    row = fetch_one_dict(cur)
    cur.close()
    conn.close()
    return {"count": int(row["count"]) if row else 0}


@app.post("/room/create")
async def create_room(request: Request):
    data = await request.json()
    user_name, user_email, subject = clean_user(data)

    conn = connect_db()
    cur = dict_cursor(conn)

    try:
        room_code = create_unique_room(cur)
        members = [{"name": user_name, "email": user_email, "subject": subject}]
        cur.execute(
            """
            INSERT INTO rooms (code, host, subject, members, messages, created_at, active)
            VALUES (%s, %s, %s, %s, %s, %s, TRUE)
            """,
            (
                room_code,
                user_name,
                subject,
                json.dumps(members),
                json.dumps([]),
                datetime.now(),
            ),
        )
        conn.commit()
        return {"success": True, "room_code": room_code}
    finally:
        cur.close()
        conn.close()


@app.post("/room/join")
async def join_room(request: Request):
    data = await request.json()
    user_name, user_email, subject = clean_user(data)
    room_code = (data.get("room_code") or "").upper().strip()

    conn = connect_db()
    cur = dict_cursor(conn)

    try:
        cur.execute("SELECT * FROM rooms WHERE code = %s AND active = TRUE", (room_code,))
        room = fetch_one_dict(cur)

        if not room:
            return {"success": False, "error": "Room not found"}

        members = parse_json(room["members"], [])

        if any(member.get("email") == user_email for member in members):
            return {"success": True, "room_code": room_code}

        if len(members) >= 3:
            return {"success": False, "error": "Room full. Maximum 3 students allowed."}

        members.append({"name": user_name, "email": user_email, "subject": subject})
        cur.execute(
            "UPDATE rooms SET members = %s WHERE code = %s",
            (json.dumps(members), room_code),
        )
        conn.commit()
        return {"success": True, "room_code": room_code}
    finally:
        cur.close()
        conn.close()


@app.get("/room/{room_code}/info")
async def get_room_info(room_code: str):
    conn = connect_db()
    cur = dict_cursor(conn)
    cur.execute("SELECT * FROM rooms WHERE code = %s AND active = TRUE", (room_code.upper(),))
    room = fetch_one_dict(cur)
    cur.close()
    conn.close()

    if not room:
        return {"success": False, "error": "Room not found"}

    return {
        "success": True,
        "room": {
            "code": room["code"],
            "host": room["host"],
            "subject": room["subject"],
            "members": parse_json(room["members"], []),
        },
    }


@app.get("/chat/{room_code}")
async def get_messages(room_code: str):
    conn = connect_db()
    cur = dict_cursor(conn)
    cur.execute("SELECT messages FROM rooms WHERE code = %s AND active = TRUE", (room_code.upper(),))
    room = fetch_one_dict(cur)
    cur.close()
    conn.close()

    return {"success": True, "messages": parse_json(room["messages"], []) if room else []}


if __name__ == "__main__":
    import uvicorn

    print("Study Buddy MySQL server: http://127.0.0.1:8000")
    uvicorn.run(socket_app, host="127.0.0.1", port=8000)
