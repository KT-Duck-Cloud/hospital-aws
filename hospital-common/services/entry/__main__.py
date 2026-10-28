"""python -m services.entry serve | watch | hs-guard | hs-watch | hs-push | promote | <CLI 동작>   (root)
ENTRY_CONF (json): role · sock · group · peer_ssh · peer_health [url…] · hs_store"""
import grp
import json
import os
import subprocess
import sys
import time

from common import rpc
from common.host import Host
from services.entry.entry import Entry, main as cli, watch
from services.hsha import ha
from services.hsha.node import HsNode


def build(c):
    h = Host()
    log = lambda m: print(time.strftime("%F %T"), m, flush=True)  # noqa: E731
    node = HsNode(c["role"], h, store=c.get("hs_store", "/var/lib/hsha"))
    ssh = c.get("peer_ssh")          # 예: ["ssh", "-o", "BatchMode=yes", "peer-gw"] — 상대 쪽은 강제 명령(entry CLI)만 허용

    def peer(verb, by=""):
        r = subprocess.run(ssh + ["--by", by, verb], capture_output=True, text=True, timeout=30)
        return json.loads(r.stdout or "{}")

    def push(blob, at):
        subprocess.run(ssh + ["hs-receive", str(at)], input=blob, check=True, timeout=60)

    guard = ha.Guard(node, c.get("peer_health", []), log)
    e = Entry(c["role"], h, peer if ssh else None, replicate=(lambda: ha.replicate(node, push)) if ssh else None, guard=guard, log=log)
    return e, node, guard, push, log


def main():
    with open(os.environ.get("ENTRY_CONF", "/etc/hospital-common/entry.json")) as f:
        c = json.load(f)
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"
    e, node, guard, push, log = build(c)
    if cmd == "serve":
        gid = grp.getgrnam(c.get("group", "console")).gr_gid
        rpc.serve(c.get("sock", "/run/entry/agent.sock"), e.ops(), 0o660, gid, log)
        while True:
            time.sleep(3600)
    run = {"watch": lambda: watch(e, _count("/run/entry-watch.count")), "hs-guard": guard.check,
           "hs-watch": lambda: ha.Watch(node, c.get("peer_health", []), lambda: e.start({"by": "hs-watch"}), log=log,
                                         state=_count("/run/hs-watch.count")).tick(),
           "hs-push": lambda: ha.replicate(node, push), "promote": lambda: ha.promote(node, c.get("peer_health", []), "--force" in sys.argv, log),
           "hs-receive": lambda: node.receive(sys.stdin.buffer.read(), float(sys.argv[2]))}
    if cmd not in run:
        return cli()
    print(json.dumps(run[cmd](), ensure_ascii=False, default=str))


class _count(dict):
    """타이머로 도는 감시의 연속 실패 수 — /run 파일에 남긴다"""
    def __init__(self, path):
        super().__init__(n=int(open(path).read() or 0) if os.path.exists(path) else 0)
        self.path = path

    def __setitem__(self, k, v):
        super().__setitem__(k, v)
        with open(self.path, "w") as f:
            f.write(str(v))


if __name__ == "__main__":
    main()
