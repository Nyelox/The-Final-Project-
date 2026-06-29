import os
import base64
import uuid
import threading
from datetime import datetime, timedelta

from flask import Flask, request, jsonify
import pymysql
from werkzeug.utils import secure_filename

from Server.Database_connection import handle_login, handle_signup
from supabase import create_client, Client
from Server.server_crypto import generate_rsa_keys, export_public_key, rsa_decrypt, aes_decrypt
from Server.socket_server import notification_hub   # ערוץ הסוקט הגולמי לדחיפת התראות

SUPABASE_URL = "https://trgaimvzokzrtapgkxsd.supabase.co"
# מפתח הגישה ל-Supabase Storage
SUPABASE_SERVICE_KEY = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6InRyZ2FpbXZ6b2t6cnRhcGdreHNkIiwicm9sZSI6InNlcnZpY2Vfcm9sZSIsImlhdCI6MTc2NTQ3MDQ2NSwiZXhwIjoyMDgxMDQ2NDY1fQ.8VFdJPQEmCsMqnnAUBGYFuG0tUtPzOroSx6hnKEF2og"
SUPABASE_BUCKET = "Files" # שם ה-Bucket בשרת

supabase: Client = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)

APP_HOST = "0.0.0.0"
APP_PORT = 5000

UPLOAD_DIR = "uploaded_files"
MAX_FILE_MB = 50

online_users = {}
ONLINE_TIMEOUT_SECONDS = 20

DB_CONFIG = dict(
    host='localhost',
    user='root',
    password='1234',
    database='userdata',
    charset='utf8mb4',
    cursorclass=pymysql.cursors.DictCursor
)

app = Flask(__name__)
# הגבלת גודל הקובץ המקסימלי להעלאה (50MB)
app.config["MAX_CONTENT_LENGTH"] = MAX_FILE_MB * 1024 * 1024

# ייצור מפתחות RSA לשרת
_rsa_private, _rsa_public = generate_rsa_keys()
print("RSA Key generated.")

# מילון שמירת מפתחות AES של כל לקוח מחובר (token -> aes_key)
_sessions = {}




# התחברות לבסיס הנתונים MySQL
def get_db():
    return pymysql.connect(**DB_CONFIG)


# יצירת טבלאות בסיס הנתונים במידה והן לא קיימות
def init_db():
    con = get_db()
    cur = con.cursor()

    # טבלת קבצים משותפים
    cur.execute("""
        CREATE TABLE IF NOT EXISTS shared_files (
            id INT AUTO_INCREMENT PRIMARY KEY,
            file_uid VARCHAR(64) NOT NULL,
            sender VARCHAR(255) NOT NULL,
            receiver VARCHAR(255) NOT NULL,
            filename VARCHAR(255) NOT NULL,
            path VARCHAR(500) NOT NULL,
            expires_at DATETIME NOT NULL,
            max_downloads INT DEFAULT 1,
            download_count INT DEFAULT 0,
            uploaded_at DATETIME DEFAULT CURRENT_TIMESTAMP
        );
    """)

    # בדיקה והוספת עמודות במידה וצריך (עבור עדכוני גרסה)
    cur.execute("SHOW COLUMNS FROM shared_files LIKE 'max_downloads'")
    if not cur.fetchone():
        cur.execute("ALTER TABLE shared_files ADD COLUMN max_downloads INT DEFAULT 1")

    cur.execute("SHOW COLUMNS FROM shared_files LIKE 'download_count'")
    if not cur.fetchone():
        cur.execute("ALTER TABLE shared_files ADD COLUMN download_count INT DEFAULT 0")

    # טבלת היסטוריית פעולות
    cur.execute("""
        CREATE TABLE IF NOT EXISTS history (
            id INT AUTO_INCREMENT PRIMARY KEY,
            username VARCHAR(255) NOT NULL,
            action VARCHAR(255) NOT NULL,
            details TEXT,
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
        );
    """)

    # טבלת הגדרות מערכת
    cur.execute("""
        CREATE TABLE IF NOT EXISTS system_settings (
            setting_key VARCHAR(50) PRIMARY KEY,
            setting_value VARCHAR(255)
        );
    """)

    # הכנסת הגדרת ברירת מחדל להורדות אם לא קיימת
    cur.execute("SELECT setting_value FROM system_settings WHERE setting_key='global_max_downloads'")
    if not cur.fetchone():
        cur.execute("INSERT INTO system_settings (setting_key, setting_value) VALUES ('global_max_downloads', '5')")

    # טבלת עבודות הדפסה
    cur.execute("""
        CREATE TABLE IF NOT EXISTS print_jobs (
            id            INT AUTO_INCREMENT PRIMARY KEY,
            file_id       INT NOT NULL,
            sender        VARCHAR(255) NOT NULL,
            filename      VARCHAR(255) NOT NULL,
            file_type     VARCHAR(20) NOT NULL,
            print_allowed BOOLEAN DEFAULT 0,
            print_status  ENUM('pending','printed') DEFAULT 'pending',
            created_at    DATETIME DEFAULT CURRENT_TIMESTAMP
        );
    """)

    # פונקציית עזר להוספת עמודות לטבלאות קיימות
    def add_column(table, col, defi):
        try:
            cur.execute(f"ALTER TABLE {table} ADD COLUMN {col} {defi}")
        except Exception:
            pass # העמודה כנראה כבר קיימת

    add_column("data", "is_blocked", "BOOLEAN DEFAULT 0")
    add_column("data", "is_admin", "BOOLEAN DEFAULT 0")
    add_column("shared_files", "message", "TEXT")
    add_column("shared_files", "encrypted_aes_key", "TEXT")  # מפתח AES המוצפן של כל קובץ

    con.commit()
    con.close()


# רישום פעולה להיסטוריית המערכת
def log_history(username, action, details=""):
    try:
        con = get_db()
        cur = con.cursor()
        cur.execute("INSERT INTO history (username, action, details) VALUES (%s, %s, %s)",
                    (username, action, details))
        con.commit()
        con.close()
    except Exception as e:
        print(f"Failed to log history: {e}")


# ניקוי קבצים שפג תוקפם מהשרת ומבסיס הנתונים
def cleanup_expired_files():
    con = get_db()
    cur = con.cursor()

    cur.execute("SELECT id, path FROM shared_files WHERE expires_at < NOW()")
    expired = cur.fetchall()

    for row in expired:
        # הסרה מ-Supabase Storage
        try:
            supabase.storage.from_(SUPABASE_BUCKET).remove([row["path"]])
        except Exception as e:
            print(f"Error removing file from Supabase: {e}")

        # ניסיון ניקוי מקומי (למקרה של קבצים ישנים)
        try:
            if os.path.exists(row["path"]):
                os.remove(row["path"])
        except Exception:
            pass

        cur.execute("DELETE FROM shared_files WHERE id=%s", (row["id"],))

    con.commit()
    con.close()


# הרצת ניקוי קבצים ברקע כל 5 דקות (מופעל מתוך main, אחרי init_db)
def _cleanup_loop():
    # המתנה ראשונית כדי לתת ל-DB להתאתחל
    threading.Event().wait(10)
    while True:
        try:
            cleanup_expired_files()
        except Exception as e:
            print(f"Cleanup error: {e}")
        threading.Event().wait(300)  # 5 minutes


def start_cleanup_thread():
    t = threading.Thread(target=_cleanup_loop, daemon=True)
    t.start()


# שליחת המפתח הציבורי RSA ללקוח
@app.route("/public_key", methods=["GET"])
def public_key():
    return jsonify({"public_key": export_public_key(_rsa_public)})


# קבלת מפתח AES מוצפן מהלקוח ושמירתו בזיכרון
@app.route("/session_key", methods=["POST"])
def session_key():
    data = request.json
    encrypted_aes = data.get("encrypted_key", "")
    try:
        aes_key = rsa_decrypt(_rsa_private, encrypted_aes)
        token = uuid.uuid4().hex
        _sessions[token] = aes_key
        print(f"AES key established for session {token[:8]}...")
        return jsonify({"session_token": token})
    except Exception as e:
        return jsonify({"status": "Key exchange failed"}), 400


# נקודת קצה להרשמת משתמש חדש
@app.route("/signup", methods=["POST"])
def api_signup():
    # פענוח הנתונים המוצפנים
    body = request.json
    token = body.get("session_token", "")
    if token not in _sessions:
        return jsonify({"status": "Invalid session"}), 401
    try:
        data = aes_decrypt(_sessions[token], body.get("encrypted_data", ""))
    except Exception:
        return jsonify({"status": "Decryption failed"}), 400

    username = data.get("username", "").strip()
    password = data.get("password", "").strip()
    if not username or not password:
        return jsonify({"status": "Missing fields"}), 400

    res = handle_signup(username, password)
    if res.get("status") == "success":
        log_history(username, "SIGNUP", "User created account")
        return jsonify({"status": res["message"]})

    return jsonify({"status": res.get("message", "Error")})


# נקודת קצה להתחברות משתמש
@app.route("/login", methods=["POST"])
def api_login():
    # פענוח הנתונים המוצפנים
    body = request.json
    token = body.get("session_token", "")
    if token not in _sessions:
        return jsonify({"status": "Invalid session"}), 401
    try:
        data = aes_decrypt(_sessions[token], body.get("encrypted_data", ""))
    except Exception:
        return jsonify({"status": "Decryption failed"}), 400

    username = data.get("username", "").strip()
    password = data.get("password", "").strip()
    if not username or not password:
        return jsonify({"status": "Missing fields"}), 400

    res = handle_login(username, password)

    if res.get("status") == "success":
        log_history(username, "LOGIN", "User logged in")
        return jsonify({
            "status": res["message"],
            "is_admin": res.get("is_admin", False),
            "is_blocked": res.get("is_blocked", False)
        })

    return jsonify({"status": res.get("message", "Login failed")})


# העלאת קובץ חדש ושיתופו עם משתמשים אחרים
@app.route("/upload_file", methods=["POST"])
def upload_file():
    data = request.json
    sender = data.get("sender", "").strip()

    # תמיכה גם בשם בודד וגם ברשימת נמענים
    receivers = data.get("receivers", [])
    if not isinstance(receivers, list):
        receivers = [str(receivers).strip()]

    single_receiver = data.get("receiver", "").strip()
    if single_receiver and single_receiver not in receivers:
        receivers.append(single_receiver)

    filename = data.get("filename", "").strip()
    filedata_b64 = data.get("filedata", "")
    encrypted_aes_key = data.get("encrypted_aes_key", "")
    minutes = int(data.get("minutes", 10))
    max_downloads = int(data.get("max_downloads", 1))

    message = data.get("message", "").strip()

    if not all([sender, receivers, filename, encrypted_aes_key]) or "filedata" not in data:
        return jsonify({"status": "Missing fields"}), 400

    safe_name = secure_filename(filename)
    if not safe_name:
        return jsonify({"status": "Bad filename"}), 400

    try:
        raw_bytes = base64.b64decode(filedata_b64)
    except Exception:
        return jsonify({"status": "Invalid base64"}), 400

    # בדיקת גודל הקובץ בצד השרת (גיבוי אבטחתי - לא סומכים רק על הלקוח)
    if len(raw_bytes) > MAX_FILE_MB * 1024 * 1024:
        return jsonify({"status": f"File too large (max {MAX_FILE_MB} MB)"}), 413

    file_uid = uuid.uuid4().hex
    server_filename = f"{file_uid}_{safe_name}"
    path = server_filename

    con = get_db()
    cur = con.cursor()

    # Validate that all receivers exist
    invalid_receivers = []
    for rcv in receivers:
        rcv = rcv.strip()
        if not rcv: continue
        cur.execute("SELECT username FROM data WHERE username=%s", (rcv,))
        if not cur.fetchone():
            invalid_receivers.append(rcv)

    if invalid_receivers:
        con.close()
        return jsonify({"status": f"User(s) not found: {', '.join(invalid_receivers)}"}), 404

    # Proceed with Supabase upload
    try:
        supabase.storage.from_(SUPABASE_BUCKET).upload(path, raw_bytes, {"content-type": "application/octet-stream"})
    except Exception as e:
        con.close()
        print(f"Supabase upload error: {e}")
        return jsonify({"status": "Storage Error"}), 500

    expires_at = datetime.now() + timedelta(minutes=minutes)

    for rcv in receivers:
        rcv = rcv.strip()
        if not rcv:
            continue
        cur.execute("""
            INSERT INTO shared_files(file_uid, sender, receiver, filename, path, expires_at, max_downloads, download_count, message, encrypted_aes_key)
            VALUES (%s, %s, %s, %s, %s, %s, %s, 0, %s, %s)
        """, (file_uid, sender, rcv, safe_name, path, expires_at, max_downloads, message, encrypted_aes_key))
        log_history(sender, "UPLOAD", f"Sent file '{safe_name}' to {rcv} (Max downloads: {max_downloads})")

        # דחיפת התראה בזמן אמת לנמען דרך ערוץ הסוקט (אם הוא מחובר)
        try:
            notification_hub.push_to_user(rcv, {
                "type": "FILE_RECEIVED",
                "sender": sender,
                "filename": safe_name
            })
        except Exception as e:
            print(f"[socket] push failed: {e}")

    con.commit()
    con.close()

    return jsonify({"status": "OK"})


# שליפת רשימת הקבצים שמחכים למשתמש מסוים
@app.route("/incoming_files", methods=["POST"])
def incoming_files():
    data = request.json
    receiver = data.get("receiver", "").strip()
    if not receiver:
        return jsonify({"status": "Missing receiver"}), 400

    con = get_db()
    cur = con.cursor()
    # שליפת קבצים שעדיין לא פג תוקפם ולא הגיעו למקסימום הורדות
    cur.execute("""
        SELECT id, sender, filename, uploaded_at, expires_at, max_downloads, download_count, message
        FROM shared_files
        WHERE receiver=%s AND expires_at >= NOW() AND download_count < max_downloads
        ORDER BY uploaded_at DESC
    """, (receiver,))
    rows = cur.fetchall()
    con.close()

    return jsonify({"status": "OK", "files": rows})


# הורדת תוכן קובץ (Base64) ועדכון מונה הורדות
@app.route("/get_file", methods=["POST"])
def get_file():
    """
    JSON: { receiver, file_id }
    """
    data = request.json
    receiver = data.get("receiver", "").strip()
    file_id = data.get("file_id")

    if not receiver or not file_id:
        return jsonify({"status": "Missing fields"}), 400

    con = get_db()
    cur = con.cursor()

    # 1. שליפת הגבלת ההורדות הגלובלית
    cur.execute("SELECT setting_value FROM system_settings WHERE setting_key='global_max_downloads'")
    # 2. שליפת פרטי הקובץ כולל המפתח המוצפן
    cur.execute("""
        SELECT filename, path, expires_at, encrypted_aes_key
        FROM shared_files
        WHERE id=%s AND receiver=%s
    """, (file_id, receiver))
    row = cur.fetchone()

    if not row:
        con.close()
        return jsonify({"status": "Not found"}), 404

    if datetime.now() > row["expires_at"]:
        con.close()
        return jsonify({"status": "File expired"}), 403

    cur.execute("UPDATE shared_files SET download_count = download_count + 1 WHERE id=%s", (file_id,))
    con.commit()

    # 4. בדיקה האם הגענו למקסימום הורדות
    cur.execute("SELECT download_count, max_downloads, path FROM shared_files WHERE id=%s", (file_id,))
    limit_check = cur.fetchone()

    should_delete = False
    if limit_check and limit_check["download_count"] >= limit_check["max_downloads"]:
        should_delete = True

    con.close()

    # הורדת הקובץ מ-Supabase
    try:
        response = supabase.storage.from_(SUPABASE_BUCKET).download(row["path"])
        raw = response
    except Exception as e:
        print(f"Supabase download error: {e}")
        if os.path.exists(row["path"]):
            with open(row["path"], "rb") as f:
                raw = f.read()
        else:
            return jsonify({"status": "File not found"}), 404

    # 5. ניקוי סופי אם עברנו את הגבלת ההורדות
    if should_delete:
        # מחיקת הרשומה הספציפית מהטבלה
        try:
            _delete_file_internal(file_id)
        except:
            pass

    encoded = base64.b64encode(raw).decode()

    # פענוח מפתח ה-AES של הקובץ עם RSA הפרטי של השרת
    file_aes_key = rsa_decrypt(_rsa_private, row["encrypted_aes_key"])
    file_aes_key_b64 = base64.b64encode(file_aes_key).decode()

    log_history(receiver, "DOWNLOAD", f"Downloaded file '{row['filename']}' ({limit_check['download_count']}/{limit_check['max_downloads']})")

    return jsonify({
        "status": "OK",
        "filename": row["filename"],
        "filedata": encoded,
        "file_aes_key": file_aes_key_b64
    })

# מחיקת קובץ מהשרת ומבסיס הנתונים באופן פנימי
def _delete_file_internal(file_id):
    con = get_db()
    cur = con.cursor()
    cur.execute("SELECT path FROM shared_files WHERE id=%s", (file_id,))
    row = cur.fetchone()
    if row:
        path = row["path"]
        # מחיקה מבסיס הנתונים
        cur.execute("DELETE FROM shared_files WHERE id=%s", (file_id,))
        # בדיקה האם יש קבצים אחרים שמשתמשים באותו נתיב לפני מחיקה מהאחסון
        cur.execute("SELECT COUNT(*) as count FROM shared_files WHERE path=%s", (path,))
        if cur.fetchone()["count"] == 0:
            try:
                supabase.storage.from_(SUPABASE_BUCKET).remove([path])
            except:
                pass
    con.commit()
    con.close()



# שליפת כל הקבצים ששותפו במערכת (עבור המפעיל/אדמין)
@app.route("/all_sent_files", methods=["GET"])
def all_sent_files():
    """
    Operator view: returns ALL shared_files rows (no receiver filter).
    Optional query param ?admin_user=<username> for future auth gating.
    """
    con = get_db()
    cur = con.cursor()
    cur.execute("""
        SELECT id, sender, receiver, filename, path, uploaded_at, expires_at, max_downloads, download_count, message
        FROM shared_files
        ORDER BY uploaded_at DESC
    """)
    rows = cur.fetchall()
    con.close()

    # המרת תאריכים לטקסט עבור פורמט JSON
    for r in rows:
        if r.get("uploaded_at"):
            r["uploaded_at"] = str(r["uploaded_at"])
        if r.get("expires_at"):
            r["expires_at"] = str(r["expires_at"])

    return jsonify({"status": "OK", "files": rows})


# בקשת קובץ להדפסה ויצירת "עבודת הדפסה" חדשה
@app.route("/request_print", methods=["POST"])
def request_print():
    """
    JSON: { file_id, operator }
    Downloads file bytes from Supabase, returns base64,
    and inserts a 'pending' print_jobs record.
    """
    data = request.json
    file_id = data.get("file_id")
    operator = data.get("operator", "operator").strip()

    if not file_id:
        return jsonify({"status": "Missing file_id"}), 400

    con = get_db()
    cur = con.cursor()
    cur.execute("""
        SELECT id, sender, filename, path, encrypted_aes_key
        FROM shared_files
        WHERE id=%s
    """, (file_id,))
    row = cur.fetchone()

    if not row:
        con.close()
        return jsonify({"status": "File not found"}), 404

    filename = row["filename"]
    ext = os.path.splitext(filename)[1].lower().lstrip(".")
    print_allowed = ext in ("pdf", "docx")

    # הורדה מ-Supabase
    try:
        raw = supabase.storage.from_(SUPABASE_BUCKET).download(row["path"])
    except Exception as e:
        con.close()
        print(f"Supabase download error: {e}")
        return jsonify({"status": "Storage error"}), 500

    # הכנסת עבודת הדפסה במצב ממתין (Pending)
    cur.execute("""
        INSERT INTO print_jobs (file_id, sender, filename, file_type, print_allowed, print_status)
        VALUES (%s, %s, %s, %s, %s, 'pending')
    """, (file_id, row["sender"], filename, ext, int(print_allowed)))
    job_id = cur.lastrowid
    con.commit()
    con.close()

    encoded = base64.b64encode(raw).decode()

    # פענוח מפתח ה-AES של הקובץ עם RSA הפרטי של השרת
    file_aes_key = rsa_decrypt(_rsa_private, row["encrypted_aes_key"])
    file_aes_key_b64 = base64.b64encode(file_aes_key).decode()

    log_history(operator, "PRINT_REQUEST", f"Requested print of '{filename}' (job #{job_id})")

    return jsonify({
        "status": "OK",
        "job_id": job_id,
        "filename": filename,
        "file_type": ext,
        "filedata": encoded,
        "file_aes_key": file_aes_key_b64
    })


# עדכון סטטוס הדפסה (ממתין -> הודפס)
@app.route("/update_print_status", methods=["POST"])
def update_print_status():
    """
    JSON: { job_id, status }  where status is 'pending' or 'printed'
    """
    data = request.json
    job_id = data.get("job_id")
    status = data.get("status", "printed").strip()

    if not job_id or status not in ("pending", "printed"):
        return jsonify({"status": "Missing or invalid fields"}), 400

    con = get_db()
    cur = con.cursor()
    cur.execute("UPDATE print_jobs SET print_status=%s WHERE id=%s", (status, job_id))
    con.commit()
    con.close()

    return jsonify({"status": "OK"})


# שליפת רשימת כל שמות המשתמשים במערכת
@app.route("/all_users", methods=["GET"])
def all_users():
    con = get_db()
    cur = con.cursor()
    cur.execute("SELECT username FROM data ORDER BY username")
    rows = cur.fetchall()
    con.close()

    users = [r["username"] for r in rows]
    return jsonify({"status": "OK", "users": users})


# עדכון נוכחות המשתמש כ-Online
@app.route("/user_online", methods=["POST"])
def user_online():
    data = request.json
    username = data.get("username", "").strip()
    if not username:
        return jsonify({"status": "Missing username"}), 400

    online_users[username] = datetime.now()
    return jsonify({"status": "OK"})


# שליפת רשימת המשתמשים שנראו לאחרונה
@app.route("/online_users", methods=["GET"])
def online_users_list():
    now = datetime.now()
    active = [
        u for u, t in online_users.items()
        if (now - t).total_seconds() < ONLINE_TIMEOUT_SECONDS
    ]
    return jsonify({"status": "OK", "online": active})

# --- Admin Endpoints ---

# בדיקה האם המשתמש הוא מנהל מערכת (Admin)
def is_admin(username):
    con = get_db()
    cur = con.cursor()
    cur.execute("SELECT is_admin FROM data WHERE username=%s", (username,))
    res = cur.fetchone()
    con.close()
    return res and res["is_admin"]

# ניהול משתמשים - שליפת כל המשתמשים עם סטטוס חסימה (לאדמין)
@app.route("/admin/users", methods=["POST"])
def admin_users():
    data = request.json
    admin_user = data.get("admin_user", "")

    if not is_admin(admin_user):
        return jsonify({"status": "Forbidden"}), 403

    con = get_db()
    cur = con.cursor()
    cur.execute("SELECT username, is_blocked, is_admin FROM data ORDER BY username")
    rows = cur.fetchall()
    con.close()

    for r in rows:
        r["is_blocked"] = bool(r["is_blocked"])
        r["is_admin"] = bool(r["is_admin"])

    return jsonify({"status": "OK", "users": rows})

# חסימה או שחרור של משתמש מהמערכת (לאדמין)
@app.route("/admin/block_user", methods=["POST"])
def admin_block_user():
    data = request.json
    admin_user = data.get("admin_user", "")
    target_user = data.get("target_user", "")
    block = data.get("block", False)

    if not is_admin(admin_user):
        return jsonify({"status": "Forbidden"}), 403

    if admin_user == target_user:
        return jsonify({"status": "Cannot block self"}), 400

    con = get_db()
    cur = con.cursor()
    cur.execute("UPDATE data SET is_blocked=%s WHERE username=%s", (1 if block else 0, target_user))
    con.commit()
    con.close()

    action = "BLOCKED" if block else "UNBLOCKED"
    log_history(admin_user, "ADMIN_ACTION", f"{action} user {target_user}")

    return jsonify({"status": "OK"})

# שליפת היסטוריית הפעולות במערכת (לאדמין)
@app.route("/admin/history", methods=["POST"])
def admin_history():
    data = request.json
    admin_user = data.get("admin_user", "")
    target_user = data.get("target_user", None)

    if not is_admin(admin_user):
        return jsonify({"status": "Forbidden"}), 403

    con = get_db()
    cur = con.cursor()

    if target_user:
        cur.execute("SELECT * FROM history WHERE username=%s ORDER BY timestamp DESC", (target_user,))
    else:
        cur.execute("SELECT * FROM history ORDER BY timestamp DESC")

    rows = cur.fetchall()
    con.close()

    return jsonify({"status": "OK", "history": rows})



if __name__ == "__main__":
    init_db() # אתחול בסיס הנתונים
    start_cleanup_thread()  # ניקוי תקופתי ברקע
    # הפעלת ערוץ הסוקט הגולמי במקביל ל-Flask (פורט נפרד 5001)
    notification_hub.start(host=APP_HOST, port=5001)
    # threaded=True - מאפשר לFlask לטפל במספר בקשות במקביל
    app.run(host=APP_HOST, port=APP_PORT, debug=False, threaded=True, use_reloader=False)
