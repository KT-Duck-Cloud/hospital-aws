"""서비스 사이 호출 — unix socket 한 줄 JSON. 요청 {"op", "args"} · 응답 {"ok", "result" | "error"}.
네트워크 포트를 열지 않고, 파일 권한(0660 + 그룹)으로 부를 수 있는 서비스를 정한다. 허용 목록(ops)에 없는 동작은 거부."""
import json
import os
import socket
import socketserver
import threading


class _Server(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True


def serve(path, ops, mode=0o660, gid=None, log=lambda m: None):
    class H(socketserver.StreamRequestHandler):
        def handle(self):
            try:
                req = json.loads(self.rfile.readline(65536) or b"{}")
                op = req.get("op")
                if op not in ops:
                    raise ValueError(f"허용되지 않은 동작: {op}")
                if not op.endswith(("status", "list")):
                    log(f"요청 {op} {json.dumps(req.get('args') or {}, ensure_ascii=False)}")
                res = {"ok": True, "result": ops[op](req.get("args") or {})}
            except Exception as e:
                res = {"ok": False, "error": str(e)}
            self.wfile.write((json.dumps(res, ensure_ascii=False, default=str) + "\n").encode())

    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    if os.path.exists(path):
        os.unlink(path)
    srv = _Server(path, H)
    if gid is not None:
        os.chown(path, 0, gid)
    os.chmod(path, mode)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def call(path, op, timeout=60, **args):
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        s.connect(path)
        s.sendall((json.dumps({"op": op, "args": args}) + "\n").encode())
        buf = b""
        while not buf.endswith(b"\n"):
            chunk = s.recv(65536)
            if not chunk:
                break
            buf += chunk
    finally:
        s.close()
    res = json.loads(buf or b"{}")
    if not res.get("ok"):
        raise RuntimeError(res.get("error") or "응답 없음")
    return res["result"]


def client(path, timeout=60):
    return lambda op, **a: call(path, op, timeout, **a)
