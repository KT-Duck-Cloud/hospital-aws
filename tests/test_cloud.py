import base64
import json
import os
import shutil
import tempfile
import time
import unittest

from cloud.api import Cloud
from cloud.otp import Otp, hotp
from dashboard import acl as ACL
from dashboard import admin
from pilotlight import control, flows
from sim import drill
from sim import scenario as sc
from sim.aws import FakeAws

QUIET = lambda *a, **k: None  # noqa: E731
SECRET = base64.b32encode(b"0123456789abcdefghij").decode()


class TestCloud(unittest.TestCase):
    def setUp(self):
        self.c, self.clock, self.ctx, self.j = drill.world()
        flows.door(self.ctx)
        flows.prepare(self.ctx)
        self.clock.sleep(5)
        self.j.tick()
        self.now = [1_900_000_000.0]
        self.cloud = Cloud(FakeAws(self.c), dr=lambda: control.state(self.ctx), otp=Otp(secret=SECRET, clock=lambda: self.now[0]))

    def code(self):
        return hotp(SECRET, int(self.now[0] // 30))

    def test_standby_then_failover(self):
        s = self.cloud.site.summary()
        self.assertEqual((s["mode"], [n["state"] for n in s["nodes"]]), ("대기(Pilot Light)", ["stopped", "running"]))
        snap = self.cloud.billing.snapshot(force=True)
        self.assertEqual({f["kind"] for f in snap["features"]}, {"ec2", "asg", "fixed"})
        self.assertGreater(snap["totals"]["fixed_month"], 0)
        for n in (1, 2):
            ok, msg = sc.scenario(n, self.c, self.clock, self.ctx, self.j, QUIET)
            self.assertTrue(ok, msg)
        s = self.cloud.site.summary()
        self.assertEqual((s["mode"], s["running"], s["asg"]["desired"]), ("운영(1조 승격)", 2, self.ctx.cfg.aws_workers))
        self.assertFalse(self.cloud.site.power("off")["success"])
        self.assertFalse(self.cloud.billing.master("off")["success"])
        asg = next(f for f in self.cloud.billing.snapshot(force=True)["features"] if f["kind"] == "asg")
        self.assertIn("0 으로", self.cloud.billing.toggle(asg["key"], "scale", 0)["error"])

    def test_otp_guard(self):
        code, body, _ = self.cloud.post("/api/aws/power/on", {}, {})
        self.assertEqual((code, body.get("otp_required")), (403, True))
        self.assertEqual(self.cloud.post("/api/otp/verify", {"code": "000000"}, {})[0], 400)
        code, body, hdr = self.cloud.post("/api/otp/verify", {"code": self.code()}, {})
        self.assertEqual(code, 200)
        self.assertIn("HttpOnly", hdr[0][1])
        h = {"X-Duck-OTP": body["token"]}
        self.assertTrue(self.cloud.get("/api/otp/status", {}, h)[1]["authenticated"])
        self.assertTrue(self.cloud.post("/api/aws/power/on", {}, h)[1]["success"])
        self.assertTrue(self.c.apps[sc.AWS_APP])
        self.now[0] += 901
        self.assertEqual(self.cloud.post("/api/aws/power/off", {}, h)[0], 403)
        self.assertFalse(self.cloud.billing.toggle("ec2:x:i-0nope", "off")["success"])
        for _ in range(5):
            self.cloud.otp.verify("111111")
        self.assertIn("5번", self.cloud.otp.verify(self.code())[1])

    def test_demo_instances(self):
        h = {"X-Duck-OTP": self.cloud.post("/api/otp/verify", {"code": self.code()}, {})[1]["token"]}
        self.assertEqual(self.cloud.post("/api/demo/instance", {"name": "w"}, {})[0], 403)
        code, r, _ = self.cloud.post("/api/demo/instance", {"name": "worker-a", "type": "t3.medium"}, h)
        self.assertEqual(code, 200)
        iid = r["instance"]["id"]
        feats = {f["key"]: f for f in self.cloud.billing.snapshot(force=True)["features"]}
        f = feats[f"ec2:ap-southeast-2:{iid}"]
        self.assertEqual((f["on"], f["role"], f["warn"]), (True, "extra", False))
        self.assertTrue(self.cloud.post("/api/billing/toggle", {"key": f["key"], "action": "off"}, h)[1]["success"])
        self.assertFalse(self.cloud.aws.extras()[0]["on"])
        self.assertEqual(self.cloud.post("/api/demo/instance", {"name": "worker-a"}, h)[0], 400)
        self.assertEqual(self.cloud.post("/api/demo/instance/remove", {"id": self.cloud.aws.vms["vm1"]["id"]}, h)[0], 400)
        self.assertTrue(self.cloud.post("/api/demo/instance/remove", {"id": iid}, h)[1]["success"])
        self.assertEqual(self.cloud.get("/api/demo/instances", {}, {})[1]["instances"], [])

    def test_pilot_light(self):
        calls = []
        self.cloud.pilot = lambda a: (calls.append(a), (True, ["ok"]))[1]
        h = {"X-Duck-OTP": self.cloud.post("/api/otp/verify", {"code": self.code()}, {})[1]["token"]}
        st = self.cloud.get("/api/pilot-light/status", {}, {})[1]
        self.assertEqual((st["mode"], st["ec2"]["vm1"]["state"], st["dbs"]["aws"]["read_only"]), ("PILOT_LIGHT_STANDBY", "stopped", True))
        self.assertTrue(self.cloud.post("/api/pilot-light/action", {"action": "promote"}, h)[1]["success"])
        self.assertEqual(calls, ["promote"])
        self.c.apps[sc.AWS_APP] = True
        self.assertEqual(self.cloud.pl_status()["mode"], "WARM_STANDBY")
        r = self.cloud.post("/api/pilot-light/action", {"action": "pilot"}, h)[1]
        self.assertTrue(r["success"] and not self.c.apps[sc.AWS_APP])

    def test_pages_data(self):
        for p in ("/api/aws/services", "/api/connection", "/api/billing/migration-estimate", "/api/billing/budget"):
            code, body, _ = self.cloud.get(p, {}, {})
            self.assertEqual(code, 200, p)
        svc = self.cloud.get("/api/aws/services", {}, {})[1]
        self.assertTrue(svc["catalog"] and svc["tech"])
        tid = svc["tech"][0]["id"]
        self.assertTrue(self.cloud.get("/api/aws/tech", {"id": tid}, {})[1]["success"])
        con = self.cloud.get("/api/connection", {}, {})[1]
        self.assertGreater(next(a for a in con["apis"] if a["api"] == "ec2 describe-instances")["count"], 0)
        self.assertIn("ec2:StopInstances", con["iam_policy"])

    def test_admin_dr(self):
        st = {"dr": control.state(self.ctx), "errors": [],
              "probes": [{"kind": "db", "host": h, "ok": True} for h in (sc.T3, sc.T5, sc.AWS_DB)]}
        d = admin.dr_api(st)
        self.assertTrue(d["ok"])
        self.assertEqual([c["k"] for c in d["crit"]][:4], ["1조 DB 응답", "대기 DB 응답", "역할 준비", "복제 IO · SQL"])
        self.assertTrue(d["crit"][3]["ok"])
        self.assertFalse(admin.dr_api({"dr": None, "errors": ["x"]})["ok"])


class TestAcl(unittest.TestCase):
    def setUp(self):
        d = tempfile.mkdtemp()
        self.path = os.path.join(d, "acl.hujson")
        shutil.copy(os.path.join(os.path.dirname(__file__), "..", "vpn", "acl.hujson"), self.path)
        self.pol = ACL.Policy(self.path, clock=time.time)
        self.nodes = lambda: [{"name": "t3-vm2", "ip": "100.64.0.32", "user": "t3", "tags": []},
                              {"name": "t8-vm2", "ip": "100.64.0.82", "user": "t8", "tags": []}]

    def test_edit_preview_apply(self):
        code, live = ACL.api(self.pol, self.nodes, {"op": "load"}, "ops")
        self.assertEqual(code, 200)
        n = len(live["model"]["rules"])
        _, e = ACL.api(self.pol, self.nodes, {"op": "edit", "kind": "add", "content": live["live"]["content"],
                                              "rule": {"src": "t8@", "dst": "100.64.0.32:3306", "comment": "AWS 복제"}}, "ops")
        self.assertEqual(len(e["model"]["rules"]), n + 1)
        self.assertIn("// AWS 복제", e["content"])
        _, t = ACL.api(self.pol, self.nodes, {"op": "test", "content": e["content"], "src": "t8-vm2", "dst": "100.64.0.32", "port": 3306}, "ops")
        self.assertTrue(t["allowed"])
        _, p = ACL.api(self.pol, self.nodes, {"op": "preview", "content": e["content"]}, "ops")
        self.assertTrue(any(x.startswith("+") and "3306" in x for x in p["diff"]))
        self.assertEqual(ACL.api(self.pol, self.nodes, {"op": "apply", "content": e["content"], "base_sha": "old"}, "ops")[0], 400)
        _, a = ACL.api(self.pol, self.nodes, {"op": "apply", "content": e["content"], "base_sha": live["live"]["sha"]}, "ops")
        self.assertEqual((a["changed"], a["version"]), (True, 1))
        self.assertEqual(self.pol.version(1)["content"], live["live"]["content"])
        code, bad = ACL.api(self.pol, self.nodes, {"op": "edit", "kind": "add", "content": e["content"], "rule": {"src": "*", "dst": "t3@"}}, "ops")
        self.assertIn("포트", bad["error"])


class TestIntegration(unittest.TestCase):
    """다른 저장소와 맞닿는 곳 — duck-scorer → DR 컨트롤, VPN 가입 서버 → Prometheus"""

    def test_scorer_message(self):
        _, _, ctx, _ = drill.world()
        msg = {"target": {"site": "on-prem", "ip": "100.64.0.51", "host": "t5-vm1"}, "failed": {"ip": "100.64.0.31", "host": "t3-vm1"},
               "timestamp": "2026-10-25T10:00:00+09:00"}
        self.assertEqual(control.gateways(ctx, msg), {"accepted": True, "duplicate": False, "action": "advisory"})
        self.assertTrue(control.gateways(ctx, msg)["duplicate"])
        self.assertIn("점수 서버 추천", ctx.store.data["history"][0]["text"])
        self.assertEqual(control.state(ctx)["scorer"]["target"]["host"], "t5-vm1")
        self.assertFalse(ctx.store.dr["active"])
        for bad in ({**msg, "target": {**msg["target"], "host": "t5-vm2"}}, {**msg, "failed": {"ip": "8.8.8.8", "host": "t3-vm1"}},
                    {**msg, "target": {**msg["target"], "site": "gcp"}}, {}):
            with self.assertRaises(ValueError):
                control.gateways(ctx, bad)

    def test_prometheus_targets(self):
        from tests.test_stack import vpn_world
        from vpn import join
        _, hs, a = vpn_world()
        for h in ("t5-vm1", "t5-vm2"):
            hs.register(a.join_key({"slot": 5})["key"], h)
        a.watch_once()
        site = join.Site({"db": os.path.join(tempfile.mkdtemp(), "j.db"), "login_server": "https://vpn.example.org",
                          "public_origin": "https://vpn.example.org", "members": []}, call=lambda op, **k: a.ops()[op](k))
        code, body, _, _ = site.get("/join/targets.json", None, "exporter=mysqld")
        self.assertEqual((code, json.loads(body)), (200, [{"targets": ["100.64.0.52:9104"], "labels": {"team": "t5", "host": "t5-vm2", "vm": "vm2"}}]))
        self.assertEqual(len(json.loads(site.get("/join/targets.json", None, "")[1])), 2)
        self.assertEqual(site.get("/join/targets.json", None, "exporter=x")[0], 400)


if __name__ == "__main__":
    unittest.main()
