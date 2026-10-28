import http.client
import json
import os
import tempfile
import unittest
import urllib.parse

from dashboard import board
from dashboard import server as dash
from pilotlight import control
from sim import drill, scenario, stack
from sim.headscale import FakeHeadscale
from vpn import agent, join

QUIET = dict(say=lambda *_: None)


class Clock:
    t = 1000.0

    def __call__(self):
        return self.t


def vpn_world():
    clock = Clock()
    hs = FakeHeadscale(clock)
    for s in (5, 8):
        hs.add_user(f"t{s}")
    a = agent.Agent(hs, {"slots": [5, 8], "login_server": "https://vpn.example.org", "protected": {"8": [9]}}, clock=clock)
    return clock, hs, a


class VpnAgent(unittest.TestCase):
    def test_join_then_auto_fix(self):
        _, hs, a = vpn_world()
        hs.register(a.join_key({"slot": 5})["key"], "t5-vm2")
        self.assertEqual(hs.ip_of("t5-vm2"), "100.64.0.100")
        self.assertEqual([r["ip"] for r in a.watch_once()], ["100.64.0.52"])
        self.assertEqual(a.watch_once(), [])
        ov = a.overview()["members"][0]["vms"][0]
        self.assertTrue(ov["ip_ok"] and ov["vm"] == 2)

    def test_key_is_one_time_and_rate_limited(self):
        clock, hs, a = vpn_world()
        k = a.join_key({"slot": 5})["key"]
        hs.register(k, "t5-vm1")
        with self.assertRaises(PermissionError):
            hs.register(k, "t5-vm3")
        for _ in range(9):
            a.join_key({"slot": 5})
        with self.assertRaises(ValueError):
            a.join_key({"slot": 5})
        clock.t += 3601
        a.join_key({"slot": 5})

    def test_guards(self):
        _, hs, a = vpn_world()
        hs.register(a.join_key({"slot": 5})["key"], "t5-vm1")
        with self.assertRaises(ValueError):
            a.join_key({"slot": 4})
        with self.assertRaises(ValueError):
            a.fix_ip({"slot": 8, "vm": 9})
        with self.assertRaises(ValueError):
            a.node_delete({"slot": 5, "vm": 1})          # 온라인
        hs._nodes[0]["online"] = False
        self.assertEqual(a.node_delete({"slot": 5, "vm": 1}), {"deleted": 1})

    def test_ip_conflict_refused(self):
        _, hs, a = vpn_world()
        hs.register(a.join_key({"slot": 5})["key"], "t5-vm1")
        hs.register(a.join_key({"slot": 5})["key"], "t5-vm1-old")
        hs._nodes[1]["ip"] = "100.64.0.51"
        with self.assertRaises(ValueError):
            a.fix_ip({"slot": 5, "vm": 1})

    def test_socket_roundtrip_and_ro_ops(self):
        _, _, a = vpn_world()
        d = tempfile.mkdtemp(prefix="pl")
        rw, ro = os.path.join(d, "a"), os.path.join(d, "r")
        s1, s2 = agent.serve(rw, a.ops(), 0o600), agent.serve(ro, a.ops(ro=True), 0o600)
        try:
            self.assertIn("key", agent.call(rw, "join_key", slot=5))
            with self.assertRaises(RuntimeError):
                agent.call(ro, "join_key", slot=5)
            self.assertEqual(len(agent.call(ro, "overview")["members"]), 2)
        finally:
            for s in (s1, s2):
                s.shutdown()
                s.server_close()


class JoinSite(unittest.TestCase):
    def setUp(self):
        _, self.hs, a = vpn_world()
        d = tempfile.mkdtemp()
        self.site = join.Site({"db": os.path.join(d, "j.db"), "login_server": "https://vpn.example.org", "public_origin": "https://vpn.example.org",
                               "members": [{"email": "A@Example.org", "name": "김 오리", "slot": 5}]},
                              call=lambda op, **k: a.ops()[op](k))

    def claim(self, pw="correct horse battery"):
        code, body, _ = self.site.post("/join/claim", {"email": "a@example.org", "name": "김  오리"}, "1.1.1.1", "")
        self.assertEqual(code, 200)
        tok = body.decode().split('name="t" value="')[1].split('"')[0]
        code, _, hdr = self.site.post("/join/setpw", {"t": tok, "pw": pw, "pw2": pw}, "1.1.1.1", "")
        self.assertEqual(code, 303)
        return dict(hdr)["Set-Cookie"].split(";")[0]

    def test_claim_login_key(self):
        cookie = self.claim()
        code, body, _ = self.site.post("/join/key", {"vm": "1"}, "1.1.1.1", cookie)
        self.assertIn("install.sh | sudo bash -s -- t5-vm1", body.decode())
        self.assertEqual(len(self.hs.keys), 1)
        code, _, _ = self.site.post("/join/claim", {"email": "a@example.org", "name": "김 오리"}, "2.2.2.2", "")
        self.assertEqual(code, 401)                               # 이미 등록

    def test_lockout_and_rate_limit(self):
        self.claim()
        for i in range(5):
            self.assertEqual(self.site.post("/join/login", {"email": "a@example.org", "pw": "x"}, f"9.9.9.{i}", "")[0], 401)
        self.assertEqual(self.site.post("/join/login", {"email": "a@example.org", "pw": "correct horse battery"}, "8.8.8.8", "")[0], 401)
        for _ in range(10):
            self.site.post("/join/login", {"email": "nobody@example.org", "pw": "x"}, "7.7.7.7", "")
        self.assertEqual(self.site.post("/join/login", {"email": "a@example.org", "pw": "x"}, "7.7.7.7", "")[0], 429)

    def test_install_script_served_without_secrets(self):
        code, body, ctype, _ = self.site.get("/join/install.sh", None)
        self.assertEqual(code, 200)
        self.assertIn(b"LOGIN=https://vpn.example.org", body)
        self.assertNotIn(b"__LOGIN_SERVER__", body)


class ControlAndDashboard(unittest.TestCase):
    def test_control_api(self):
        c, clock, ctx, j = drill.world()
        srv = control.serve(ctx, j, "127.0.0.1:0", "t" * 40)
        port = srv.server_address[1]
        try:
            def req(method, path, body=None, token="t" * 40):
                con = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
                con.request(method, path, json.dumps(body) if body else None, {"Authorization": f"Bearer {token}"})
                r = con.getresponse()
                out = r.status, json.loads(r.read())
                con.close()
                return out
            self.assertEqual(req("GET", "/api/state", token="bad")[0], 401)
            self.assertEqual(req("POST", "/api/action", {"op": "mode", "mode": "off"})[0], 200)
            self.assertEqual(req("POST", "/api/action", {"op": "mode", "mode": "yolo"})[0], 400)
            self.assertEqual(req("POST", "/api/action", {"op": "prepare"})[0], 200)
            code, st = req("GET", "/api/state")
            self.assertEqual((code, st["mode"], st["prepared"]["ok"]), (200, "off", True))
        finally:
            srv.shutdown()
            srv.server_close()

    def test_phase_and_errors(self):
        self.assertEqual(dash.phase(None)["key"], "unknown")
        self.assertEqual(dash.phase({"dr": {"active": {"since": 1}, "back_to": "x"}, "job": {}})["key"], "failback")
        col = dash.Collector(lambda: (_ for _ in ()).throw(OSError("down")), lambda: {"members": []}, None)
        st = col.status()
        self.assertEqual(st["phase"]["key"], "unknown")
        self.assertTrue(st["errors"][0].startswith("DR 컨트롤"))

    def test_board_phases_follow_scenarios(self):
        c, clock, ctx, j = drill.world()
        from pilotlight import flows
        from sim.cluster import Probe
        flows.prepare(ctx)
        col = dash.Collector(lambda: control.state(ctx), lambda: None, Probe(c), ttl=0)
        seen = []
        for n in (0, 1, 2, 3, 4):
            if n:
                self.assertTrue(scenario.scenario(n, c, clock, ctx, j)[0])
            j.tick()
            st = col.status()
            b = board.build(st["dr"], st["probes"])
            seen.append((b["phase"], b["act"], b["upstream"]))
            self.assertEqual(scenario.NEXT[b["phase"]], n % 4 + 1)
        self.assertEqual(seen, [("NORMAL", "t3", "t3"), ("DR1", "t5", "t5"), ("DR2", "aws", "aws"),
                                ("RECOVERING", "aws", "aws"), ("RECOVERED", "t3", "t3")])

    def test_pages_and_drill_auth(self):
        calls = []
        col = dash.Collector(lambda: None, lambda: None, None)
        srv = dash.http.server.ThreadingHTTPServer(("127.0.0.1", 0), dash.handler(
            col, [dash.ipaddress.ip_network("127.0.0.1/32")], lambda a: (calls.append(a), (True, "ok"))[1], "pw"))
        dash.threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            def req(method, path, headers=None):
                con = http.client.HTTPConnection("127.0.0.1", srv.server_address[1], timeout=10)
                con.request(method, path, b"{}" if method == "POST" else None, headers or {})
                r = con.getresponse()
                out = r.status, r.read()
                con.close()
                return out
            self.assertIn("DuckCloud 서비스 상태".encode(), req("GET", "/")[1])
            self.assertIn("DuckCloud DR 현황".encode(), req("GET", "/dr")[1])
            self.assertIn("DuckCloud DR 현황".encode(), req("GET", "/", {"Host": "dr.example.org"})[1])
            for path, title in (("/admin", "DuckCloud 관리"), ("/admin/home", "전체 현황"), ("/admin/aws", "2조 AWS"),
                                ("/aws/", "AWS 관리 대시보드"), ("/aws/aws-connection", "<html"), ("/admin/excluded/dev", "포함하지 않았습니다"),
                                ("/master", "과금 · 콘솔"), ("/master/otp", "<html"), ("/master/excluded/console", "포함하지 않았습니다"),
                                ("/hs/join", "VPN 가입"), ("/hs/join/vm", "가입 키 발급"), ("/hs/join/mesh", "메쉬 편성 현황")):
                code, body = req("GET", path)
                self.assertEqual(code, 200, path)
                self.assertIn(title.encode(), body, path)
            self.assertEqual(req("GET", "/api/drboard")[0], 503)
            self.assertEqual(req("POST", "/api/drill/scenario-1", {"X-Dr-Auth": "x"})[0], 403)
            self.assertEqual(req("POST", "/api/drill/scenario-1", {"X-Dr-Auth": "pw"})[0], 200)
            self.assertEqual(calls, ["scenario-1"])
        finally:
            srv.shutdown()
            srv.server_close()

    def test_full_stack(self):
        ctx, st = stack.main(["--pace", "0", "--base", "18490"], **QUIET)
        self.assertEqual(st["phase"]["key"], "normal")
        self.assertEqual(st["errors"], [])
        vms = [v for m in st["vpn"]["members"] for v in m["vms"]]
        self.assertEqual(len(vms), 6)
        self.assertTrue(all(v["ip_ok"] for v in vms))


if __name__ == "__main__":
    unittest.main()
