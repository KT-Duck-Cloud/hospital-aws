"""전체 연결 시연 — 가짜 인프라 위에 진짜 서버들을 localhost 로 띄운다.

  VPN 에이전트   unix socket (가짜 Headscale)        ← 가입 화면 · 통합 관제가 부른다
  가입 화면      http://127.0.0.1:8096/join          팀원 첫 등록 → 로그인 → 가입 키 → (가짜) tailscale up → 자동 주소 고정
  DR 컨트롤 API  http://127.0.0.1:8090/api/state     Bearer 토큰 (실행할 때 앞 8자만 출력)
  서비스 상태    http://127.0.0.1:8095/              운영 화면과 같은 디자인 (정적)
  DR 현황        http://127.0.0.1:8095/dr            4초마다 /api/drboard — 시연 시나리오 버튼 ①~④ · 리셋 (비밀번호는 실행할 때 출력)
  AWS 대시보드   http://127.0.0.1:8095/aws/          가짜 AWS(sim/aws.py) — EC2 상태가 DR 승격 · 복귀를 따라간다. 과금 · 전원 제어는 OTP
  관리 사이트    http://127.0.0.1:8095/admin         DR 칸(/dr/api) · 보안(ACL 편집 · 경로 시험 · 적용 — 임시 사본에)
  관리 · 과금    http://127.0.0.1:8095/admin  /master  /aws/  /hs/join   운영 사이트별 화면 (sites/, 디자인만)

  python3 -m sim.stack                 자동 훈련을 천천히 (약 1분) — DR 현황 화면으로 보기
  python3 -m sim.stack --manual        자동 훈련 없이 대기 — DR 현황 화면에서 ① → ② → ③ → ④ 를 직접 누르기 (Ctrl-C 로 끝)
  python3 -m sim.stack --hold          자동 훈련이 끝난 뒤에도 유지 (버튼으로 다시 해 보기)
  python3 -m sim.stack --pace 0        바로 끝까지 (시험용)
"""
import argparse
import base64
import http.server
import ipaddress
import os
import secrets
import shutil
import tempfile
import threading
import time

from cloud.api import Cloud
from cloud.otp import Otp
from dashboard import acl as ACL
from dashboard import board
from dashboard import server as dash
from pilotlight import control, flows
from sim import drill
from sim.aws import FakeAws
from sim import scenario as sc
from sim.cluster import Probe
from sim.headscale import FakeHeadscale
from vpn import agent as vpn_agent
from vpn import join

ROSTER = [{"email": "site1@example.org", "name": "1조 운영", "slot": 3}, {"email": "site1b@example.org", "name": "1조 대기", "slot": 5},
          {"email": "aws@example.org", "name": "AWS 담당", "slot": 8}]


def serve(port, h):
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", port), h)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def main(argv=None, say=print):
    ap = argparse.ArgumentParser()
    ap.add_argument("--pace", type=float, default=0.04)
    ap.add_argument("--hold", action="store_true", help="끝난 뒤 Ctrl-C 까지 서버 유지")
    ap.add_argument("--manual", action="store_true", help="자동 훈련 없이 버튼으로")
    ap.add_argument("--base", type=int, default=8090)
    a = ap.parse_args(argv)
    tmp = tempfile.mkdtemp(prefix="pl")
    token, password = secrets.token_hex(32), secrets.token_urlsafe(6)
    lock, auto = threading.Lock(), {"on": not a.manual}
    box = {}

    def on_ready(c, clock, ctx, j):
        def online(ip):
            return c.servers[ip].up if ip in c.servers else bool(c.apps.get(ip, True))
        hs = FakeHeadscale(clock.now, online)
        for s in (3, 5, 8):
            hs.add_user(f"t{s}")
        conf = {"slots": [3, 5, 8], "login_server": "https://vpn.example.org", "control_ip": "100.64.0.1",
                "labels": {"3": "1조 운영", "5": "1조 대기", "8": "AWS"}}
        ag = vpn_agent.Agent(hs, conf, clock=clock.now)
        sock, ro = os.path.join(tmp, "a.sock"), os.path.join(tmp, "r.sock")
        vpn_agent.serve(sock, ag.ops(), 0o600)
        vpn_agent.serve(ro, ag.ops(ro=True), 0o600)
        site = join.Site({"db": os.path.join(tmp, "join.db"), "agent_sock": sock, "login_server": conf["login_server"],
                          "public_origin": f"http://127.0.0.1:{a.base + 6}", "members": ROSTER})
        for s in (3, 5, 8):                       # 팀원이 가입 키를 받아 VM 두 대를 가입시킨 것과 같다
            for v in (1, 2):
                hs.register(vpn_agent.call(sock, "join_key", slot=s)["key"], f"t{s}-vm{v}")
        fixed = ag.watch_once()
        say(f"VPN — 가입 6대, 규칙 주소로 자동 고정 {len(fixed)}대 ({', '.join(f['from'] + '→' + f['ip'] for f in fixed)})")
        col = dash.Collector(dash.http_dr(f"http://127.0.0.1:{a.base}", token), lambda: vpn_agent.call(ro, "overview"), Probe(c), ttl=1)

        def drill_action(action):
            if auto["on"]:
                raise ValueError("자동 훈련 진행 중 — 끝난 뒤에 (--manual 로 실행하면 처음부터 버튼으로)")
            with lock:
                if action == "reset":
                    return sc.reset(c, clock, ctx, j)
                n = int(action.removeprefix("scenario-")) if action.startswith("scenario-") else 0
                st = col.status()
                want = sc.NEXT[board.build(st["dr"], st["probes"])["phase"]]
                if n != want:
                    raise ValueError(f"다음 단계는 {want}번입니다")
                ok, msg = sc.scenario(n, c, clock, ctx, j)
                steps = "\n".join(f"  {t:4}s {m}" for job in ctx.runner.jobs[-2:] for t, m in job["steps"]) if n in (2, 4) else ""
                return ok, msg + ("\n" + steps if steps else "") + f"\n가상 시각 t+{clock.now():.0f}s"

        otp = base64.b32encode(secrets.token_bytes(20)).decode()
        def phase():
            st = col.status()
            return board.build(st["dr"], st["probes"]) or {}

        def pilot(action):
            """AWS 화면의 Pilot Light 버튼 → DR 시연 단계 (promote: ①② · failback: ③④)"""
            plan = {"promote": {"NORMAL": [1, 2], "RECOVERED": [1, 2], "DR1": [2]}, "failback": {"DR2": [3, 4], "RECOVERING": [4]}}[action]
            col.at, p = 0, phase().get("phase")
            if p not in plan:
                raise ValueError(f"지금 단계({p})에서는 {action} 을 할 수 없습니다")
            logs = []
            for n in plan[p]:
                ok, msg = drill_action(f"scenario-{n}")
                col.at = 0
                logs += [f"[{n}] " + x for x in msg.splitlines()]
                if not ok:
                    return False, logs
            return True, logs

        cloud = Cloud(FakeAws(c), dr=lambda: control.state(ctx), board=phase, pilot=pilot,
                      otp=Otp(secret=otp, user="ops@example.org", clock=time.time))
        shutil.copy(os.path.join(os.path.dirname(vpn_agent.__file__), "acl.hujson"), os.path.join(tmp, "acl.hujson"))
        policy = ACL.Policy(os.path.join(tmp, "acl.hujson"))
        box.update(col=col, otp=otp, cloud=cloud, servers=[serve(a.base + 6, join.handler(site)), control.serve(ctx, j, f"127.0.0.1:{a.base}", token),
                                     serve(a.base + 5, dash.handler(col, [ipaddress.ip_network("127.0.0.1/32")], drill_action, password,
                                                                    cloud, policy, acl_apply=True))])
        say(f"서비스 상태  http://127.0.0.1:{a.base + 5}/\nDR 현황      http://127.0.0.1:{a.base + 5}/dr   (시연 버튼 비밀번호: {password})\n"
            f"관리 · 과금  http://127.0.0.1:{a.base + 5}/admin  /master  /aws/  /hs/join   (AWS OTP 데모 코드는 AWS 화면 아래 데모 칸에 표시)\n"
            f"가입 화면    http://127.0.0.1:{a.base + 6}/join\nDR 컨트롤    http://127.0.0.1:{a.base}/api/state  (Bearer {token[:8]}…)\n")
        if a.pace and not a.manual:
            say("DR 현황 화면을 열어 두세요 — 5초 뒤 시작")
            time.sleep(5)

    def hold(c, clock, ctx, j):
        auto["on"] = False
        say("버튼으로 다시 해 볼 수 있습니다 — Ctrl-C 로 끝")
        try:
            while True:
                with lock:
                    j.tick()
                clock.sleep(ctx.cfg.interval_sec) if a.pace else time.sleep(1)
        except KeyboardInterrupt:
            pass

    if a.manual:
        c, clock, ctx, j = drill.world(a.pace or 0.04)
        on_ready(c, clock, ctx, j)
        flows.door(ctx)
        flows.prepare(ctx)
        hold(c, clock, ctx, j)
    else:
        ctx = drill.main(say=say, pace=a.pace, on_ready=on_ready, hold=hold if a.hold else None)
    st = box["col"].status()
    say(f"\n관제 요약 — {st['phase']['text']} · VPN 노드 {sum(len(m['vms']) for m in (st.get('vpn') or {}).get('members', []))}대 · "
        f"오류 {st.get('errors')}")
    for s in box.get("servers", []):
        s.shutdown()
        s.server_close()
    return ctx, st


if __name__ == "__main__":
    main()
