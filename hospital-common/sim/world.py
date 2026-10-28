"""두 게이트웨이(S1 노트북 서버 · S2 클라우드) + 1조 호스트 VM 을 임시 폴더에 세우고, 서비스들을 unix socket 으로 잇는다.

  S1  10.10.10.8 (WireGuard) · 203.0.113.10 (공인)      entry · Headscale(대기) · front
  S2  10.10.10.3 · 203.0.113.20                         entry · Headscale(메인)
  1조 호스트  hostagent — t3-vm1(보호: control-plane · DB) · t3-vm2(worker)
  콘솔        S1 에서 → hostagent socket · entry socket
"""
import contextlib
import os
import sqlite3
import tempfile

from common import rpc
from common.audit import Audit
from common.auth import Accounts
from services.console.app import Console
from services.entry.entry import Entry
from services.front.app import Front
from services.hostagent.agent import HostAgent
from services.hsha import ha
from services.hsha.node import HsNode
from sim.fake import Clock, FakeHost, FakeVmm, Net

ADDR = {"S1": ("10.10.10.8", "203.0.113.10"), "S2": ("10.10.10.3", "203.0.113.20")}
PEER = {"S1": "S2", "S2": "S1"}
HOST_CONF = {"vms": {"t3-vm1": {"role": "control-plane · MySQL", "protected": True}, "t3-vm2": {"role": "worker"}},
             "limits": {"cpus_min": 2, "cpus_max": 8, "mem_mb_min": 2048, "mem_mb_max": 12288, "disk_gb_max": 120,
                        "host_mem_reserve_mb": 4096, "host_disk_reserve_gb": 15}}
FRONTDOOR = "upstream hospital_active {{\n    server {}:30080 max_fails=1 fail_timeout=5s;\n}}\n"
LABELS = {"100.64.0.31": "온프레미스 t3 (주 운영)", "100.64.0.51": "온프레미스 t5 (1차 DR)", "100.64.0.81": "AWS t8 (2차 DR)"}


def health(role):
    return [f"https://{a}:63728/health" for a in ADDR[role]]


@contextlib.contextmanager
def hs_db(node):
    c = sqlite3.connect(os.path.join(node.lib, "db.sqlite"))
    try:
        with c:
            yield c
    finally:
        c.close()


def join(node, name, ip):
    """Headscale 에 노드 가입 — 주 서버 DB 에만 들어간다"""
    if not node.running():
        raise RuntimeError(f"{node.role} Headscale 이 주 서버가 아님")
    with hs_db(node) as c:
        c.execute("INSERT INTO nodes VALUES (?,?)", (name, ip))


def members(node):
    with hs_db(node) as c:
        return sorted(r[0] for r in c.execute("SELECT name FROM nodes"))


class World:
    def __init__(self, iters=1000):
        self.tmp, self.clock, self.net = tempfile.mkdtemp(prefix="hc"), Clock(), Net()
        self.log = []
        say = self.log.append
        self.gw, self.node, self.entry, self.guard = {}, {}, {}, {}
        for r in ("S1", "S2"):
            h = FakeHost(r, self.net, self.clock, ("cloudflared-front" if r == "S1" else "cloudflared", "nginx", "headscale"))
            h.connector, h.mesh = ("cloudflared-front" if r == "S1" else "cloudflared"), os.path.join(self.tmp, r, "etc/nginx/hc-mesh/inner-listen.conf")
            for a in ADDR[r]:
                self.net.addr[a] = h
            self.gw[r] = h
            n = HsNode(r, h, os.path.join(self.tmp, r))
            os.makedirs(n.etc), os.makedirs(n.lib)
            h.write(os.path.join(n.etc, "config.yaml"), f"server_url: https://vpn.example.org\n# {r}\n")
            h.write(os.path.join(n.lib, "noise_private.key"), "privkey:example-only\n")
            with hs_db(n) as c:
                c.execute("CREATE TABLE nodes (name TEXT PRIMARY KEY, ip TEXT)")
            self.node[r] = n
            self.guard[r] = ha.Guard(n, health(PEER[r]), say)
            h.boot_hook = (lambda g: lambda unit: unit != "headscale" or not _refused(g))(self.guard[r])
        for r in ("S1", "S2"):
            p = PEER[r]
            self.entry[r] = Entry(r, self.gw[r], self.peer_call(p), self.gw[r].mesh, guard=self.guard[r], log=say, sleep=self.clock.sleep,
                                  replicate=lambda r=r: ha.replicate(self.node[r], self.node[PEER[r]].receive))
            self.gw[r].start("nginx")
            self.entry[r].start({"by": "setup"})
        self.gw["S2"].start("headscale")                       # S2 메인 · S1 대기
        self.gw["S1"].write(os.path.join(self.tmp, "S1/etc/nginx/sites-available/hospital-frontdoor"), FRONTDOOR.format("100.64.0.31"))
        self.net.public["https://www.example.org/api/site"] = lambda: 200

        self.vmm = FakeVmm({"t3-vm1": {"cpus": 4, "mem_mb": 8192, "disk_gb": 50, "on": True},
                            "t3-vm2": {"cpus": 2, "mem_mb": 6144, "disk_gb": 40, "on": True}}, mem_total_mb=16384)
        self.sock = {k: os.path.join(self.tmp, f"{k}.sock") for k in ("hostagent", "entry")}
        self.servers = [rpc.serve(self.sock["hostagent"], HostAgent(HOST_CONF, self.vmm, sleep=self.clock.sleep, log=say).ops(), 0o600),
                        rpc.serve(self.sock["entry"], self.entry["S1"].ops(), 0o600)]
        self.accounts = Accounts(os.path.join(self.tmp, "accounts.db"), iters=iters, clock=self.clock.now)
        self.console = Console({"allowed_cidrs": ["127.0.0.1/32", "100.64.0.0/10"], "origin": "http://127.0.0.1",
                                "public_url": "https://www.example.org/api/site", "session_minutes": 60},
                               self.accounts, rpc.client(self.sock["hostagent"]), Audit(os.path.join(self.tmp, "audit.jsonl"), self.clock.now),
                               rpc.client(self.sock["entry"]), lambda url: self.gw["S1"].http(url)[0], self.clock.now, self.clock.sleep, sync=True)
        self.front = Front({"origin": "http://127.0.0.1", "site_url": "https://www.example.org/", "labels": LABELS}, self.accounts,
                           lambda: self.gw["S1"].read(os.path.join(self.tmp, "S1/etc/nginx/sites-available/hospital-frontdoor")),
                           rpc.client(self.sock["entry"]), lambda: {"snapshot_at": self.node["S1"].latest()[1]}, self.clock.now)

    def peer_call(self, role):
        def call(verb, by=""):
            if not self.gw[role].up:
                raise RuntimeError(f"{role} 응답 없음")
            return self.entry[role].ops()[verb]({"by": by})
        return call

    def watch(self, role):
        return ha.Watch(self.node[role], health(PEER[role]), lambda: self.entry[role].start({"by": "hs-watch"}), log=self.log.append)

    def close(self):
        for s in self.servers:
            s.shutdown()
            s.server_close()


def _refused(guard):
    try:
        guard.pre()
        return False
    except RuntimeError:
        return True
