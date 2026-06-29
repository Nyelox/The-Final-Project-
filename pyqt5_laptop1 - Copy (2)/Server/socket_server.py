# -*- coding: utf-8 -*-
"""
socket_server.py - שרת TCP גולמי (Raw Socket) לדחיפת התראות בזמן אמת ללקוחות.
רץ במקביל לשרת ה-Flask, על פורט נפרד (5001).
Flask אחראי על העלאה והורדה של קבצים (בקשה-תשובה),
וערוץ הסוקט מאפשר לשרת ל"דחוף" (PUSH) הודעה ללקוח ברגע שמשהו קורה.
פרוטוקול: 4 בתים אורך (big-endian) + גוף JSON.
"""
import socket
import struct
import json
import threading


class NotificationHub:
    def __init__(self):
        self._clients = {}              # שם משתמש -> רשימת חיבורי סוקט
        self._lock = threading.Lock()
        self._server_sock = None

    def start(self, host="0.0.0.0", port=5001):
        self._server_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._server_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._server_sock.bind((host, port))
        self._server_sock.listen()
        print(f"[socket] Notification server listening on {host}:{port}")
        threading.Thread(target=self._accept_loop, daemon=True).start()

    def _accept_loop(self):
        while True:
            try:
                conn, addr = self._server_sock.accept()
                threading.Thread(target=self._handle_client,
                                 args=(conn, addr), daemon=True).start()
            except Exception as e:
                print(f"[socket] accept error: {e}")
                break

    def _handle_client(self, conn, addr):
        username = None
        try:
            while True:
                msg = _recv_json(conn)
                if msg is None:
                    break
                mtype = msg.get("type")
                if mtype == "AUTH":
                    username = msg.get("username", "").strip()
                    with self._lock:
                        self._clients.setdefault(username, []).append(conn)
                    _send_json(conn, {"type": "AUTH_OK",
                                      "message": f"מחובר לערוץ ההתראות בתור {username}"})
                    print(f"[socket] {username} connected from {addr}")
                elif mtype == "PING":
                    _send_json(conn, {"type": "PONG"})
        except Exception:
            pass
        finally:
            if username:
                with self._lock:
                    conns = self._clients.get(username, [])
                    if conn in conns:
                        conns.remove(conn)
            try:
                conn.close()
            except Exception:
                pass

    def push_to_user(self, username, event):
        with self._lock:
            conns = list(self._clients.get(username, []))
        for c in conns:
            try:
                _send_json(c, event)
            except Exception:
                pass


def _send_json(conn, obj):
    body = json.dumps(obj).encode("utf-8")
    conn.sendall(struct.pack(">I", len(body)) + body)


def _recv_exact(conn, n):
    buf = b""
    while len(buf) < n:
        chunk = conn.recv(n - len(buf))
        if not chunk:
            return None
        buf += chunk
    return buf


def _recv_json(conn):
    header = _recv_exact(conn, 4)
    if not header:
        return None
    length = struct.unpack(">I", header)[0]
    body = _recv_exact(conn, length)
    if body is None:
        return None
    return json.loads(body.decode("utf-8"))


notification_hub = NotificationHub()
