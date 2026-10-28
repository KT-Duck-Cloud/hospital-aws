import os
import tempfile
import time
import unittest

from common import rpc
from common.audit import Audit
from common.auth import Accounts, Throttle, check_password, hash_password
from common.web import Sessions, allowed, nets


class Clock:
    t = 1000.0

    def __call__(self):
        return self.t


class TestAuth(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.acc = Accounts(os.path.join(tempfile.mkdtemp(), "a.db"), iters=1000, clock=self.clock)
        self.acc.add("Ops@Example.org", "correct horse battery")

    def test_hash(self):
        h = hash_password("x" * 12, 1000)
        self.assertTrue(check_password("x" * 12, h))
        self.assertFalse(check_password("y" * 12, h))
        self.assertFalse(check_password("x", "broken"))

    def test_lockout(self):
        self.assertEqual(self.acc.verify("1.1.1.1", "ops@example.org", "correct horse battery"), "ok")
        for _ in range(5):
            self.acc.verify("1.1.1.1", "ops@example.org", "nope")
        self.assertEqual(self.acc.verify("1.1.1.1", "ops@example.org", "correct horse battery"), "locked")
        self.acc.unlock("ops@example.org")
        self.assertEqual(self.acc.verify("1.1.1.1", "ops@example.org", "correct horse battery"), "ok")
        self.assertEqual(self.acc.verify("1.1.1.1", "ghost@example.org", "x"), "bad")
        with self.assertRaises(ValueError):
            self.acc.add("short@example.org", "short")

    def test_session_ends_on_password_change(self):
        s = Sessions(60, self.acc.stamp, self.clock)
        tok, _ = s.new("ops@example.org")
        self.assertIsNotNone(s.get(tok))
        self.clock.t += 1
        self.acc.set_password("ops@example.org", "another long password")
        self.assertIsNone(s.get(tok))
        tok, _ = s.new("ops@example.org")
        self.clock.t += 61
        self.assertIsNone(s.get(tok))

    def test_throttle_and_cidr(self):
        t = Throttle(2, 60, self.clock)
        t.hit("ip"), t.hit("ip")
        self.assertTrue(t.blocked("ip"))
        self.clock.t += 61
        self.assertFalse(t.blocked("ip"))
        n = nets(["100.64.0.0/10"])
        self.assertTrue(allowed("100.64.0.31", n))
        self.assertFalse(allowed("8.8.8.8", n))
        self.assertFalse(allowed("not-ip", n))


class TestRpcAudit(unittest.TestCase):
    def test_allowlist(self):
        path = os.path.join(tempfile.mkdtemp(), "s.sock")
        srv = rpc.serve(path, {"echo": lambda a: a, "boom": lambda a: 1 / 0}, 0o600)
        try:
            self.assertEqual(rpc.call(path, "echo", x=1), {"x": 1})
            with self.assertRaisesRegex(RuntimeError, "허용되지 않은"):
                rpc.call(path, "rm")
            with self.assertRaisesRegex(RuntimeError, "division"):
                rpc.call(path, "boom")
            self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)
        finally:
            srv.shutdown()
            srv.server_close()

    def test_audit(self):
        a = Audit(os.path.join(tempfile.mkdtemp(), "x/audit.jsonl"), time.time)
        a.write("ops", "1.1.1.1", "vm_stop", {"name": "t3-vm2"}, "ok")
        a.write("ops", "1.1.1.1", "vm_start", {"name": "t3-vm2"}, "ok")
        self.assertEqual([x["action"] for x in a.tail()], ["vm_start", "vm_stop"])


if __name__ == "__main__":
    unittest.main()
