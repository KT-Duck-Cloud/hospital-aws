import os
import tempfile
import unittest

from pilotlight import cost, replica
from pilotlight.frontdoor import Frontdoor, render
from sim import drill
from sim.cluster import CTL, fmt, minus, parse

STATUS = """*************************** 1. row ***************************
             Source_Host: 100.64.0.32
      Replica_IO_Running: Connecting
     Replica_SQL_Running: Yes
   Seconds_Behind_Source: NULL
           Last_IO_Errno: 2003
           Last_IO_Error: Can't connect
      Retrieved_Gtid_Set: aaa:1-5,
bbb:1-3
       Executed_Gtid_Set: aaa:1-5
"""


class Replica(unittest.TestCase):
    def test_parse_status(self):
        st = replica.parse_status(STATUS)
        self.assertEqual((st["io"], st["sql"], st["lag"], st["errno"]), ("Connecting", "Yes", None, 2003))
        self.assertEqual(st["retrieved"], "aaa:1-5,bbb:1-3")
        self.assertFalse(replica.running(st))
        self.assertEqual(replica.parse_status(""), {"configured": False})

    def test_quote(self):
        self.assertEqual(replica.q("a'b\\c"), "'a''b\\\\c'")

    def test_choose_source_prefers_chain_standby(self):
        ok = {"io": "Yes", "sql": "Yes", "source": "w"}
        roles = [{"host": "w", "ro": False, "chain": True, "repl": {"configured": False}},
                 {"host": "s", "ro": True, "chain": True, "repl": ok}]
        self.assertEqual(replica.choose_source(roles)[0], "s")
        roles[1]["repl"] = {**ok, "io": "No"}
        self.assertEqual(replica.choose_source(roles)[0], "w")
        self.assertEqual(replica.choose_source([])[0], None)

    def test_gtid_sets(self):
        g = parse("u:1-3:5,v:2")
        self.assertEqual(fmt(g), "u:1-3:5,v:2")
        self.assertEqual(fmt(minus(g, parse("u:1-5"))), "v:2")


class Door(unittest.TestCase):
    def test_render_rejects_non_ip(self):
        with self.assertRaises(ValueError):
            render(["evil;"], [], 80)
        self.assertIn("127.0.0.1:9", render([], [], 80))

    def test_rollback_when_check_fails(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "up.conf")
            door = Frontdoor(p, 30080, sh=lambda cmd: True)
            self.assertTrue(door.point(["10.0.0.1"]))
            self.assertFalse(door.point(["10.0.0.1"]))
            with open(p) as f:
                before = f.read()
            door.sh = lambda cmd: False
            with self.assertRaises(RuntimeError):
                door.point(["10.0.0.2"])
            with open(p) as f:
                self.assertEqual(f.read(), before)


class Judge(unittest.TestCase):
    def test_full_drill(self):
        ctx = drill.main(say=lambda *_: None)
        self.assertIsNone(ctx.store.dr["active"])

    def test_no_promotion_when_replica_lagged(self):
        c, clock, ctx, j = drill.world()
        from pilotlight import flows
        flows.prepare(ctx)
        drill.run(j, clock, ctx, 1, say=lambda *_: None)
        ctx.store.data["repl_ok"]["lag"] = 120
        c.servers[drill.T5].up = c.servers[drill.T3].up = False
        clock.sleep(1)
        ctx.store.data["repl_ok"]["lag"] = 120
        drill.run(j, clock, ctx, 0.5, say=lambda *_: None)
        ctx.store.data["repl_ok"]["lag"] = 120
        drill.run(j, clock, ctx, 6, say=lambda *_: None)
        self.assertIsNone(ctx.store.dr["active"])
        self.assertIn("복제가 정상이 아니었음", ctx.store.dr["note"])

    def test_manual_stop_is_not_a_witness(self):
        c, clock, ctx, j = drill.world()
        from pilotlight import flows
        flows.prepare(ctx)
        drill.run(j, clock, ctx, 1, say=lambda *_: None)
        ctx.ex.sql(drill.AWS_DB, "STOP REPLICA;")
        for h in (drill.T5, drill.T3):
            c.partition(CTL, h)
        self.assertIn("수동 정지", j.witness())

    def test_off_mode_only_judges(self):
        c, clock, ctx, j = drill.world()
        from dataclasses import replace
        from pilotlight import flows
        ctx.cfg = replace(ctx.cfg, mode="off")
        flows.prepare(ctx)
        drill.run(j, clock, ctx, 1, say=lambda *_: None)
        c.servers[drill.T5].up = c.servers[drill.T3].up = False
        drill.run(j, clock, ctx, 8, say=lambda *_: None)
        self.assertIsNone(ctx.store.dr["active"])
        self.assertIn("AWS 승격", ctx.store.dr["note"])


class Cost(unittest.TestCase):
    def test_pilot_light_cheaper_than_warm(self):
        pilot = cost.monthly(cost.PROFILES["pilot-light"])[1]
        warm = cost.monthly(cost.PROFILES["warm-standby"])[1]
        self.assertLess(pilot, warm)
        self.assertLess(cost.failover_month(0), cost.failover_month(24))


if __name__ == "__main__":
    unittest.main()
