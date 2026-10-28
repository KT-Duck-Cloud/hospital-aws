"""전체 연결 시연 — 가짜 인프라(world.py) 위에 진짜 서비스들을 localhost 로 띄운다. Ctrl-C 로 끝.

  hostagent   unix socket  ← console
  entry (S1)  unix socket  ← console · front     (S2 는 peer 호출)
  console     http://127.0.0.1:8190/   공용 계정으로 로그인 (실행할 때 비밀번호 출력)
  front       http://127.0.0.1:8191/   공개 첫 화면 · /front/connect 로 같은 계정

  python3 -m sim.stack [--base 8190]
"""
import argparse
import http.server
import secrets
import threading
import time

from services.console.app import handler as console_handler
from services.front.app import handler as front_handler
from services.hsha import ha
from sim import world as W


def serve(port, h):
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", port), h)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def main(argv=None, say=lambda m: print(m, flush=True), hold=True):
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", type=int, default=8190)
    a = ap.parse_args(argv)
    w = W.World()
    for name, ip in (("t3-vm1", "100.64.0.31"), ("t3-vm2", "100.64.0.32")):
        W.join(w.node["S2"], name, ip)
    ha.replicate(w.node["S2"], w.node["S1"].receive)
    pw = secrets.token_urlsafe(12)
    w.accounts.add("ops@example.org", pw, "시연")
    w.console.c["origin"] = f"http://127.0.0.1:{a.base}"
    w.front.c["origin"] = f"http://127.0.0.1:{a.base + 1}"
    srv = [serve(a.base, console_handler(w.console)), serve(a.base + 1, front_handler(w.front))]
    say(f"console  http://127.0.0.1:{a.base}/\nfront    http://127.0.0.1:{a.base + 1}/\n계정     ops@example.org / {pw}\n")
    try:
        while hold:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    for s in srv:
        s.shutdown()
        s.server_close()
    w.close()
    return w, pw


if __name__ == "__main__":
    main()
