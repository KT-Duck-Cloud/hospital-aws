"""entry — 게이트웨이 공개 입구 제어 (S1 · S2 두 서버에 같은 코드, 역할만 다르다).

  ON  = 터널 커넥터(cloudflared) 켬 + 메쉬 참여 (내부 frontdoor 를 VPN :8087 에 연다 → 다른 게이트도 요청을 나눠 보냄)
  OFF = 커넥터 멈춤 + :8087 닫음. 둘 다 재부팅해도 그대로 (enable / disable)
  status        커넥터 준비(연결 수 > 0) · nginx · Headscale · 메쉬
  peer <동작>    다른 서버의 entry 를 부른다 (status | start | stop 만)
  hs_stop       Headscale 멈춤 — 주 서버였다면 마지막 사본을 먼저 넘기고, 넘기지 못하면 멈추지 않는다
  hs_start      Headscale 켬 — 시작 전 가드: 다른 서버 Headscale 이 돌고 있으면 거부 (두 주 서버 방지)
기록: 누가(--by) · 무엇을 · 언제
"""
import json
import re
import time

UNITS = {"S1": "cloudflared-front", "S2": "cloudflared"}
VIP = {"S1": "100.64.0.1", "S2": "100.64.0.2"}


class Entry:
    def __init__(self, role, host, peer=None, mesh="/etc/nginx/hc-mesh/inner-listen.conf", metrics="http://127.0.0.1:20245/ready",
                 replicate=None, guard=None, log=None, sleep=time.sleep):
        self.role, self.h, self.peer_call, self.mesh_path, self.metrics = role, host, peer, mesh, metrics
        self.replicate, self.guard, self.sleep, self.unit = replicate, guard, sleep, UNITS[role]
        self.records = []
        self.log = log or (lambda m: self.records.append(m))

    def note(self, by, what):
        self.log(f"{time.strftime('%F %T', time.localtime(self.h.now()))} {self.role} {re.sub(r'[^A-Za-z0-9@._:-]', '', by or 'root')} {what}")

    def mesh(self, on):
        old = self.h.read(self.mesh_path) or ""
        self.h.write(self.mesh_path, f"listen {VIP[self.role]}:8087;\n" if on else "")
        if not self.h.nginx_reload():
            self.h.write(self.mesh_path, old)
            raise RuntimeError("nginx -t 실패 — 메쉬 그대로")

    def status(self, a=None):
        code, body = self.h.http(self.metrics, timeout=3)
        m = re.search(rb'"readyConnections":(\d+)', body or b"")
        conns = int(m.group(1)) if code == 200 and m else 0
        return {"role": self.role, "connector": self.h.active(self.unit), "connector_ready": conns > 0, "connections": conns,
                "enabled": self.h.enabled(self.unit), "nginx": self.h.active("nginx"), "headscale": self.h.active("headscale"),
                "mesh": "listen" in (self.h.read(self.mesh_path) or ""), "at": int(self.h.now())}

    def start(self, a=None):
        self.mesh(True)
        self.h.start(self.unit)
        self.note((a or {}).get("by"), "entry start")
        self.sleep(4)
        return self.status()

    def stop(self, a=None):
        self.h.stop(self.unit)
        self.mesh(False)
        self.note((a or {}).get("by"), "entry stop")
        return self.status()

    def peer(self, a):
        verb = a.get("verb")
        if verb not in ("status", "start", "stop"):
            raise ValueError("peer 는 status | start | stop 만")
        if not self.peer_call:
            raise RuntimeError("다른 서버 연결 없음")
        if verb != "status":
            self.note(a.get("by"), f"peer {verb}")
        return self.peer_call(verb, by=f"{self.role}:{a.get('by') or 'root'}")

    def hs_stop(self, a=None):
        if self.h.active("headscale") and self.replicate:
            try:
                self.replicate()
            except Exception as e:
                raise RuntimeError(f"마지막 사본 전송 실패 — 멈추지 않음 ({e})")
        self.h.stop("headscale")
        self.note((a or {}).get("by"), "headscale stop")
        return {"headscale": False}

    def hs_start(self, a=None):
        if self.guard:
            self.guard.pre()
        self.h.start("headscale")
        self.note((a or {}).get("by"), "headscale start")
        return {"headscale": self.h.active("headscale")}

    def ops(self):
        return {"status": self.status, "start": self.start, "stop": self.stop, "peer": self.peer, "hs_stop": self.hs_stop, "hs_start": self.hs_start}


def watch(entry, state):
    """S1 감시 (1분) — S1 입구가 꺼져 있을 때 S2 입구가 2번 연속 준비 안 됨 → S2 재시작 → 그래도면 S1 입구 켬.
    state = {"n": 연속 실패 수} (실제는 /run 파일)"""
    if entry.h.active(entry.unit):
        state["n"] = 0
        return "self-on"
    ready = lambda: bool(_try(lambda: entry.peer({"verb": "status"})).get("connector_ready"))  # noqa: E731
    if ready():
        state["n"] = 0
        return "peer-ok"
    state["n"] = state.get("n", 0) + 1
    if state["n"] < 2:
        return f"peer-fail {state['n']}"
    state["n"] = 0
    _try(lambda: entry.peer({"verb": "start", "by": "watch"}))
    entry.sleep(20)
    if ready():
        entry.note("watch", "S2 재시작으로 복구")
        return "peer-restarted"
    entry.start({"by": "watch"})
    entry.note("watch", "S2 입구 응답 없음 → S1 입구 자동 켬")
    return "self-started"


def _try(fn):
    try:
        return fn() or {}
    except Exception:
        return {}


def main(argv=None):
    """duck-entry [--by 누구] <동작> [peer 동작] — 같은 서버의 entry 에이전트 socket 을 부르는 CLI"""
    import os
    import sys
    from common import rpc
    a = list(sys.argv[1:] if argv is None else argv)
    by = a[1] if a[:1] == ["--by"] else os.environ.get("SUDO_USER", "root")
    a = a[2:] if a[:1] == ["--by"] else a
    op = a[0] if a else "status"
    args = {"verb": a[1] if len(a) > 1 else "status"} if op == "peer" else {}
    print(json.dumps(rpc.call(os.environ.get("ENTRY_SOCK", "/run/entry/agent.sock"), op, 120, by=by, **args), ensure_ascii=False))
