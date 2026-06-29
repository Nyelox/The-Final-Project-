# -*- coding: utf-8 -*-
"""
socket_client.py - לקוח TCP גולמי שמתחבר לערוץ ההתראות של השרת (פורט 5001).
פותח socket ישיר, מזדהה עם שם המשתמש, ומאזין להתראות שהשרת דוחף בזמן אמת.
פרוטוקול זהה לשרת: 4 בתים אורך + גוף JSON.
"""
import socket
import struct
import json
import threading


class NotificationClient:
    def __init__(self, host, port=5001):
        self.host = host
        self.port = port
        self._sock = None
        self.on_event = None

    def connect(self, username):
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.connect((self.host, self.port))
        _send_json(self._sock, {"type": "AUTH", "username": username})
        threading.Thread(target=self._recv_loop, daemon=True).start()

    def _recv_loop(self):
        try:
            while True:
                msg = _recv_json(self._sock)
                if msg is None:
                    break
                if self.on_event:
                    self.on_event(msg)
                else:
                    print(f"[socket] התראה מהשרת: {msg}")
        except Exception:
            pass

    def close(self):
        try:
            self._sock.close()
        except Exception:
            pass


def _send_json(sock, obj):
    body = json.dumps(obj).encode("utf-8")
    sock.sendall(struct.pack(">I", len(body)) + body)


def _recv_exact(sock, n):
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            return None
        buf += chunk
    return buf


def _recv_json(sock):
    header = _recv_exact(sock, 4)
    if not header:
        return None
    length = struct.unpack(">I", header)[0]
    body = _recv_exact(sock, length)
    if body is None:
        return None
    return json.loads(body.decode("utf-8"))
