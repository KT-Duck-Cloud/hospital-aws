"""감사 기록 — 누가 · 언제 · 어디서 · 무엇을 · 결과. 한 줄 JSON 을 덧붙이기만 한다 (append-only)."""
import json
import os
import threading
import time


class Audit:
    def __init__(self, path, clock=time.time):
        self.path, self.now, self.lock = path, clock, threading.Lock()
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)

    def write(self, who, ip, action, args, result):
        rec = {"at": round(self.now(), 3), "who": who, "ip": ip, "action": action, "args": args, "result": result}
        with self.lock, open(self.path, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")
        return rec

    def tail(self, n=50):
        try:
            with open(self.path, encoding="utf-8") as f:
                return [json.loads(x) for x in f.readlines()[-n:]][::-1]
        except FileNotFoundError:
            return []
