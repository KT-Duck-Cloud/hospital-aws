"""python -m services.front — FRONT_CONF (json): listen · origin · site_url · accounts_db · frontdoor · labels {ip: 이름} · entry_sock · hs_store"""
import http.server
import json
import os

from common import rpc
from common.auth import Accounts
from common.host import Host
from services.front.app import Front, handler
from services.hsha.node import HsNode


def main():
    with open(os.environ.get("FRONT_CONF", "/etc/hospital-common/front.json")) as f:
        c = json.load(f)
    h = Host()
    node = HsNode(c.get("role", "S1"), h, store=c.get("hs_store", "/var/lib/hsha"))
    fr = Front(c, Accounts(c["accounts_db"]), lambda: h.read(c.get("frontdoor", "/etc/nginx/sites-available/hospital-frontdoor")),
               rpc.client(c["entry_sock"], 30) if c.get("entry_sock") else None, lambda: {"snapshot_at": node.latest()[1]})
    host, port = c.get("listen", "127.0.0.1:8191").rsplit(":", 1)
    print("front", host, port, flush=True)
    http.server.ThreadingHTTPServer((host, int(port)), handler(fr)).serve_forever()


if __name__ == "__main__":
    main()
