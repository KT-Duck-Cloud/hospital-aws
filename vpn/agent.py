"""VPN 서버 에이전트 (root) — 포트를 열지 않고 unix socket 한 줄 JSON 으로만 받는다.

  규칙   팀원 번호 slot(1~9) · VM 번호 vm(1~9) → 호스트 이름 t<slot>-vm<vm> · 주소 <prefix><slot><vm>  (100.64.0.52 = t5-vm2)
         대상은 slot · vm 으로만 받고 이름 · 주소 · Headscale 사용자는 여기서 계산한다 (요청이 임의 노드를 고르지 못하게)
  동작   member_status · join_key(1회용 1시간, 팀원당 시간당 10개) · fix_ip · node_delete(오프라인만) · overview
  감시   watch_once — 이름이 규칙에 맞는데 주소가 다르면 자동 고정 (같은 노드 3번 실패하면 멈춤)
  보호   설정 protected 의 (slot, vm) 은 보이기만 하고 바꾸지 않는다 (운영 노드)
"""
import json
import os
import re
import socketserver
import threading
import time

MAX_VM = 9
KEYS_PER_HOUR = 10


class Agent:
    def __init__(self, hs, conf, clock=time.time, paths=lambda: {}):
        self.hs, self.c, self.now, self.paths = hs, conf, clock, paths
        self.lock, self.keylog, self.fixfail = threading.Lock(), {}, {}

    # ---------- 규칙 ----------
    def slot(self, args):
        s = int(args.get("slot", 0))
        if s not in self.c["slots"]:
            raise ValueError("등록되지 않은 팀원 번호")
        return s

    def vm(self, args):
        v = int(args.get("vm", 0))
        if not 1 <= v <= MAX_VM:
            raise ValueError(f"vm 은 1~{MAX_VM}")
        return v

    def user_name(self, slot):
        return f"{self.c.get('user_prefix', 't')}{slot}"

    def want_ip(self, slot, vm):
        return f"{self.c.get('prefix', '100.64.0.')}{slot}{vm}"

    def vm_of(self, slot, name):
        m = re.fullmatch(rf"t{slot}-vm([1-{MAX_VM}])", name or "")
        return int(m.group(1)) if m else None

    def protected(self, slot, vm):
        return vm in (self.c.get("protected") or {}).get(str(slot), [])

    def uid(self, slot):
        name = self.user_name(slot)
        for u in self.hs.users():
            if u["name"] == name:
                return u["id"]
        raise RuntimeError(f"Headscale 사용자 {name} 없음 — headscale users create {name}")

    def nodes_of(self, slot):
        uid, ctl = self.uid(slot), self.c.get("control_ip")
        return [{**n, "vm": None if n["ip"] == ctl else (self.vm_of(slot, n["given_name"]) or self.vm_of(slot, n["hostname"]))}
                for n in self.hs.nodes() if n["user_id"] == uid]

    def find(self, slot, vm):
        hit = [n for n in self.nodes_of(slot) if n["vm"] == vm]
        if len(hit) != 1:
            raise ValueError(f"t{slot}-vm{vm} 노드가 {len(hit)}개 — " + ("--hostname 으로 가입했는지 확인" if not hit else "오프라인인 옛 노드를 먼저 삭제"))
        return hit[0]

    # ---------- 동작 ----------
    def member_status(self, args):
        s = self.slot(args)
        return {"user": self.user_name(s), "nodes": self.nodes_of(s),
                "hosts": {v: f"t{s}-vm{v}" for v in range(1, MAX_VM + 1)},
                "protected": (self.c.get("protected") or {}).get(str(s), [])}

    def join_key(self, args):
        s = self.slot(args)
        with self.lock:
            recent = [t for t in self.keylog.get(s, []) if t > self.now() - 3600]
            if len(recent) >= KEYS_PER_HOUR:
                raise ValueError(f"키는 1시간에 {KEYS_PER_HOUR}개까지")
            key = self.hs.create_key(self.uid(s), "1h")
            self.keylog[s] = recent + [self.now()]
        return {"key": key, "login_server": self.c["login_server"]}

    def fix_ip(self, args):
        s, v = self.slot(args), self.vm(args)
        if self.protected(s, v):
            raise ValueError(f"t{s}-vm{v} 는 운영 노드 — 바꾸지 않음")
        ip = self.want_ip(s, v)
        with self.lock:
            n = self.find(s, v)
            if n["ip"] == ip:
                return {"ip": ip, "changed": False}
            if not self.hs.active():
                raise ValueError("headscale 이 이 서버에서 돌고 있지 않음 (대기 서버) — 주 서버에서")
            owner = self.hs.ip_owner(ip, n["id"])
            if owner:
                raise ValueError(f"{ip} 를 다른 노드({owner})가 쓰는 중")
            self.hs.set_ipv4(n["id"], ip)
        return {"ip": ip, "changed": True, "from": n["ip"]}

    def node_delete(self, args):
        s, v = self.slot(args), self.vm(args)
        if self.protected(s, v):
            raise ValueError(f"t{s}-vm{v} 는 운영 노드 — 지우지 않음")
        with self.lock:
            hit = [n for n in self.nodes_of(s) if n["vm"] == v]
            if not hit:
                raise ValueError("삭제할 노드가 없음")
            if any(n["online"] for n in hit):
                raise ValueError("온라인 노드는 삭제할 수 없음 — VM 을 끄거나 tailscale logout 후")
            for n in hit:
                self.hs.delete_node(n["id"])
        return {"deleted": len(hit)}

    def overview(self, _args=None):
        paths, users = self.paths(), {u["id"]: u["name"] for u in self.hs.users()}
        by = {}
        for n in self.hs.nodes():
            by.setdefault(users.get(n["user_id"], ""), []).append(n)
        out = []
        for s in self.c["slots"]:
            vms = []
            for n in sorted(by.get(self.user_name(s), []), key=lambda x: x["ip"]):
                v = self.vm_of(s, n["given_name"]) or self.vm_of(s, n["hostname"])
                want = self.want_ip(s, v) if v else ""
                vms.append({"host": n["given_name"] or n["hostname"], "vm": v, "ip": n["ip"], "want_ip": want,
                            "ip_ok": bool(v) and n["ip"] == want, "online": n["online"], "last_seen": n["last_seen"],
                            "path": paths.get(n["ip"], "-"), "protected": bool(v) and self.protected(s, v)})
            out.append({"slot": s, "user": self.user_name(s), "label": (self.c.get("labels") or {}).get(str(s), ""), "vms": vms})
        return {"members": out, "at": self.now()}

    def watch_once(self):
        fixed = []
        for s in self.c["slots"]:
            try:
                nodes = self.nodes_of(s)
            except RuntimeError:
                continue
            for n in nodes:
                v = n["vm"]
                if not v or self.protected(s, v) or n["ip"] == self.want_ip(s, v) or self.fixfail.get(n["id"], 0) >= 3:
                    continue
                if not self.hs.active():
                    return fixed
                try:
                    fixed.append(self.fix_ip({"slot": s, "vm": v}))
                except Exception:
                    self.fixfail[n["id"]] = self.fixfail.get(n["id"], 0) + 1
        return fixed

    def ops(self, ro=False):
        o = {"overview": self.overview, "member_status": self.member_status}
        return o if ro else {**o, "join_key": self.join_key, "fix_ip": self.fix_ip, "node_delete": self.node_delete}


# ---------- unix socket ----------
class _Server(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True


def serve(path, ops, mode=0o660, gid=None):
    class H(socketserver.StreamRequestHandler):
        def handle(self):
            try:
                req = json.loads(self.rfile.readline(65536) or b"{}")
                if req.get("op") not in ops:
                    raise ValueError(f"허용되지 않은 동작: {req.get('op')}")
                res = {"ok": True, "result": ops[req["op"]](req.get("args") or {})}
            except Exception as e:
                res = {"ok": False, "error": str(e)}
            self.wfile.write((json.dumps(res, ensure_ascii=False) + "\n").encode())

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
    import socket
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
        raise RuntimeError(res.get("error") or "에이전트 오류")
    return res["result"]


def main():
    import grp
    from vpn.headscale import Headscale, tailscale_paths
    conf_path = os.environ.get("VPN_AGENT_CONF", "/etc/pilotlight/vpn.json")
    with open(conf_path) as f:
        conf = json.load(f)
    a = Agent(Headscale(conf.get("headscale_db", "/var/lib/headscale/db.sqlite")), conf, paths=tailscale_paths)
    serve(conf.get("sock", "/run/pilotlight-vpn/agent.sock"), a.ops(), 0o660, grp.getgrnam(conf.get("group", "vpnjoin")).gr_gid)
    serve(conf.get("ro_sock", "/run/pilotlight-vpn-ro/agent.sock"), a.ops(ro=True), 0o660,
          grp.getgrnam(conf.get("ro_group", "dashboard")).gr_gid)
    print("VPN 에이전트 시작", flush=True)
    while True:
        time.sleep(conf.get("watch_sec", 15))
        try:
            for r in a.watch_once():
                print("자동 고정", r, flush=True)
        except Exception as e:
            print("watch:", e, flush=True)


if __name__ == "__main__":
    main()
