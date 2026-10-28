import http.client
import io
import json
import os
import tarfile
import unittest
import urllib.parse

from services.hostagent.vmm import parse_vmx, patch_vmx
from sim import drill, stack
from sim import world as W

QUIET = {"say": lambda *a: None}


class TestDrill(unittest.TestCase):
    def test_drill(self):
        drill.run(**QUIET)


class TestPieces(unittest.TestCase):
    def test_vmx(self):
        t = 'numvcpus = "2"\nmemsize = "6144"\n# note\n'
        self.assertEqual(parse_vmx(t), {"numvcpus": "2", "memsize": "6144"})
        out = parse_vmx(patch_vmx(t, {"memsize": 8192, "cpuid.coresPerSocket": 2}))
        self.assertEqual((out["memsize"], out["cpuid.coresPerSocket"], out["numvcpus"]), ("8192", "2", "2"))

    def test_mesh_rollback_and_restore_guard(self):
        w = W.World()
        try:
            e = w.entry["S1"]
            before = w.gw["S1"].read(e.mesh_path)
            w.gw["S1"].nginx_ok = False                                  # nginx -t 실패 → 메쉬 설정을 되돌린다
            with self.assertRaisesRegex(RuntimeError, "nginx"):
                e.mesh(False)
            self.assertEqual(w.gw["S1"].read(e.mesh_path), before)
            self.assertIn("listen 100.64.0.1:8087", before)
            with self.assertRaisesRegex(ValueError, "peer"):
                e.peer({"verb": "hs_stop"})
            buf = io.BytesIO()
            with tarfile.open(fileobj=buf, mode="w:gz") as tar:
                data = b"x"
                info = tarfile.TarInfo("etc/passwd")
                info.size = len(data)
                tar.addfile(info, io.BytesIO(data))
            with self.assertRaisesRegex(ValueError, "허용되지 않은 경로"):
                w.node["S1"].restore(buf.getvalue())
            w.gw["S1"].start("headscale")
            with self.assertRaisesRegex(RuntimeError, "실행 중"):
                w.node["S1"].restore(buf.getvalue())
        finally:
            w.close()


class TestHttp(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.w = W.World()
        cls.w.accounts.add("ops@example.org", "correct horse battery")
        cls.w.console.c["origin"] = "http://127.0.0.1:18190"
        cls.w.front.c["origin"] = "http://127.0.0.1:18191"
        cls.srv = [stack.serve(18190, stack.console_handler(cls.w.console)), stack.serve(18191, stack.front_handler(cls.w.front))]

    @classmethod
    def tearDownClass(cls):
        for s in cls.srv:
            s.shutdown()
            s.server_close()
        cls.w.close()

    def req(self, port, method, path, body=None, headers=None, form=False):
        con = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        data = (urllib.parse.urlencode(body) if form else json.dumps(body)).encode() if body is not None else None
        h = {"Content-Type": "application/x-www-form-urlencoded" if form else "application/json", **(headers or {})}
        con.request(method, path, data, h)
        r = con.getresponse()
        out = r.status, r.read(), dict(r.getheaders())
        con.close()
        return out

    def test_console(self):
        code, _, _ = self.req(18190, "GET", "/api/state")
        self.assertEqual(code, 401)
        code, _, _ = self.req(18190, "POST", "/api/login", {"email": "ops@example.org", "pw": "x"}, {"Origin": "https://evil.example"})
        self.assertEqual(code, 403)
        code, body, hdr = self.req(18190, "POST", "/api/login", {"email": "ops@example.org", "pw": "correct horse battery"})
        self.assertEqual(code, 200)
        cookie, csrf = hdr["Set-Cookie"].split(";")[0], json.loads(body)["csrf"]
        self.assertIn("HttpOnly", hdr["Set-Cookie"])
        code, body, _ = self.req(18190, "GET", "/api/state", headers={"Cookie": cookie})
        st = json.loads(body)
        self.assertEqual([v["name"] for v in st["vms"]["vms"]], ["t3-vm1", "t3-vm2"])
        code, _, _ = self.req(18190, "POST", "/api/action", {"action": "vm_stop", "name": "t3-vm2"}, {"Cookie": cookie})
        self.assertEqual(code, 403)                                       # CSRF 없음
        code, body, _ = self.req(18190, "POST", "/api/action", {"action": "vm_stop", "name": "t3-vm1"}, {"Cookie": cookie, "X-CSRF": csrf})
        self.assertFalse(json.loads(body)["ok"])                          # 보호 VM
        code, body, _ = self.req(18190, "POST", "/api/action", {"action": "rm -rf"}, {"Cookie": cookie, "X-CSRF": csrf})
        self.assertEqual(code, 400)
        _, body, _ = self.req(18190, "GET", "/api/audit", headers={"Cookie": cookie})
        self.assertEqual([x["action"] for x in json.loads(body)][:3], ["rm -rf", "vm_stop", "login"])

    def test_front(self):
        code, body, hdr = self.req(18191, "GET", "/")
        self.assertEqual(code, 200)
        self.assertIn("온프레미스 t3".encode(), body)
        self.assertNotIn(b"100.64.0.31", body)
        self.assertEqual(self.req(18191, "GET", "/front/status")[0], 303)
        code, _, hdr = self.req(18191, "POST", "/front/connect", {"email": "ops@example.org", "pw": "correct horse battery"}, form=True)
        self.assertEqual((code, hdr["Location"]), (303, "/front/status"))
        self.assertIn("SameSite=Lax", hdr["Set-Cookie"])
        _, body, _ = self.req(18191, "GET", "/front/status", headers={"Cookie": hdr["Set-Cookie"].split(";")[0]})
        self.assertIn(b"100.64.0.31", body)


if __name__ == "__main__":
    unittest.main()
