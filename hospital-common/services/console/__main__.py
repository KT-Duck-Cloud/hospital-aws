"""python -m services.console — CONSOLE_CONF (json): listen · allowed_cidrs · origin · accounts_db · audit_log · agent_sock · entry_sock · health · public_url"""
import http.server
import json
import os

from common import rpc
from common.audit import Audit
from common.auth import Accounts
from common.host import Host
from services.console.app import Console, handler


def main():
    with open(os.environ.get("CONSOLE_CONF", "/etc/hospital-common/console.json")) as f:
        c = json.load(f)
    h = Host()
    con = Console(c, Accounts(c["accounts_db"]), rpc.client(c["agent_sock"], 900), Audit(c["audit_log"]),
                  rpc.client(c["entry_sock"], 60) if c.get("entry_sock") else None, lambda url: h.http(url, timeout=3)[0])
    host, port = c.get("listen", "127.0.0.1:8190").rsplit(":", 1)
    print("console", host, port, flush=True)
    http.server.ThreadingHTTPServer((host, int(port)), handler(con)).serve_forever()


if __name__ == "__main__":
    main()
