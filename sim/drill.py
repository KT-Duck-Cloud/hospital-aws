"""모의 훈련 — 실제 pilotlight 코드를 가짜 인프라(cluster.py) 위에서 돌린다. 사이트 배치는 운영과 같다.

  1조  t3 (주 운영, vm1 .31 앱 · vm2 .32 DB 쓰기)  →  t5 (1차 DR, .51 · .52 복제)
  2조  AWS t8 (2차 DR · Pilot Light, vm1 .81 앱 정지 · vm2 .82 DB 복제)

  [0] 준비      AWS 를 1조 대기 DB(t5, 사슬)에서 복제
  [1] 분할      판정 서버만 1조를 못 봄 — 증인(AWS IO Yes)이 승격을 막는다
  [2] ① t3 다운 → t5      AWS 는 복제 원본을 t5 로 옮긴다
  [3] ② t5 다운 → AWS     5분 뒤 펜싱 시도 → AWS 승격 → 앱 기동 → 입구 전환
  [4] ③ t3 복구           돌아온 t3 즉시 잠금 — 옛 쓰기 DB 에 기록이 들어가지 않는다
  [5] ④ t3 주 서버 복귀    역동기화 → 절체 → AWS 앱 정지 → AWS 다시 대기
"""
import sys

from pilotlight import build, flows, judge
from pilotlight.config import Config
from sim import scenario as sc
from sim.cluster import CTL, Chan, Clock, Cluster, Exec, Power, Probe

T3, T5, AWS_DB, AWS_APP = sc.T3, sc.T5, sc.AWS_DB, sc.AWS_APP
APP_USERS = ("app_runtime", "app_readonly")


def world(pace=0.0):
    c = Cluster()
    t3, t5, aws = c.add(T3, writer=True), c.add(T5), c.add(AWS_DB)
    for s in (t3, t5, aws):
        for u in APP_USERS:
            s.users[(u, "%")] = False
    t3.users[("repl_t5", T5)] = False
    for i in range(5):
        c.write(T3, "app_runtime", f"예약-{i}")
    t5.gtids = {u: set(v) for u, v in t3.gtids.items()}
    t5.rows, t5.binlog = list(t3.rows), list(t3.binlog)
    ch = t5.chans.setdefault("", Chan())
    ch.source, ch.user, ch.io, ch.sql = T3, "repl_t5", True, True
    c.step()
    c.apps.update({sc.T3_APP: True, sc.T5_APP: True, AWS_APP: False})
    clock = Clock(c, pace)
    cfg = Config(site1_dbs=(T3, T5), site1_apps=(sc.T3_APP, sc.T5_APP), standby_db=AWS_DB, standby_app=AWS_APP,
                 app_users=APP_USERS, aws_asg="pilotlight-workers", backup_dir="/var/backups/pilotlight")
    ctx = build(cfg, clock=clock, probe=Probe(c), ex=Exec(c, clock), pw=Power(c, AWS_APP), sync=True)
    return c, clock, ctx, judge.Judge(ctx)


def run(j, clock, ctx, minutes, say=print):
    sc.ticks(j, clock, ctx, lambda: False, minutes, say)


def steps(ctx, name, say):
    job = next(j for j in reversed(ctx.runner.jobs) if j["name"] == name)
    for t, m in job["steps"]:
        say(f"     {t:4}s {m}")


def act(n, c, clock, ctx, j, say):
    ok, msg = sc.scenario(n, c, clock, ctx, j, say)
    assert ok, msg
    return msg


def main(say=print, pace=0.0, on_ready=None, hold=None):
    c, clock, ctx, j = world(pace)
    if on_ready:
        on_ready(c, clock, ctx, j)
    flows.door(ctx)

    say("[0] AWS 대기 준비")
    flows.prepare(ctx)
    steps(ctx, "prepare", say)
    run(j, clock, ctx, 2, say)
    assert c.servers[AWS_DB].rows == c.servers[T3].rows, "AWS 가 1조를 따라오지 못함"
    assert ctx.store.data["prepared"]["source"] == T5, "사슬 — AWS 는 대기 DB(t5)에서 받아야 함"

    say("[1] 판정 서버 ↔ 1조 분할 (1조 · AWS 는 정상)")
    for h in (T3, T5):
        c.partition(CTL, h)
    run(j, clock, ctx, 7, say)
    assert not ctx.store.dr["active"], "분할인데 승격함 (split-brain)"
    assert "증인" in (ctx.store.dr["witness"] or {}).get("veto", "")
    for h in (T3, T5):
        c.partition(CTL, h, False)
    run(j, clock, ctx, 1, say)

    say("[2] ① t3 다운 → t5")
    say("  → " + act(1, c, clock, ctx, j, say))
    assert c.write(T5, "app_runtime", "t5운영중") == "ok"
    run(j, clock, ctx, 1, say)
    assert "t5운영중" in c.servers[AWS_DB].rows

    say("[3] ② t5 다운 → AWS")
    c.write(T5, "app_runtime", "예약-정전직전")
    clock.sleep(1)
    c.connect(T5, "app_runtime")
    say("  → " + act(2, c, clock, ctx, j, say))
    steps(ctx, "failover", say)
    assert c.apps[AWS_APP] and c.scaled[-1] == ctx.cfg.aws_workers
    assert AWS_APP in ctx.door.current
    for i in range(3):
        assert c.write(AWS_DB, "app_runtime", f"장애중-{i}") == "ok"
    assert "예약-정전직전" in c.servers[AWS_DB].rows, "RPO — 정전 직전 기록 유실"

    say("[4] ③ t3 복구 (t3 는 자기가 아직 쓰기 DB 인 줄 안다)")
    say("  → " + act(3, c, clock, ctx, j, say))
    assert c.write(T3, "app_runtime", "옛쓰기DB로-새기록") == "locked", "펜싱 실패 — 옛 쓰기 DB 에 기록됨"

    say("[5] ④ t3 주 서버 복귀")
    say("  → " + act(4, c, clock, ctx, j, say))
    steps(ctx, "failback", say)
    d = ctx.store.dr
    assert not d["active"] and d["writer"] == T3
    assert all(f"장애중-{i}" in c.servers[T3].rows for i in range(3)), "복귀 — 장애 중 기록 유실"
    assert c.write(T3, "app_runtime", "복귀후") == "ok"
    assert not c.apps[AWS_APP] and c.scaled[-1] == 0
    clock.sleep(10)
    assert "복귀후" in c.servers[AWS_DB].rows, "복귀 뒤 AWS 가 다시 대기로 따라오지 않음"
    assert c.servers[AWS_DB].sro

    say("[6] 리셋 — t5 다시 대기")
    say("  → " + sc.reset(c, clock, ctx, j)[1])
    clock.sleep(5)
    assert "복귀후" in c.servers[T5].rows
    say("\n이력")
    for h in reversed(ctx.store.data["history"]):
        say(f"  {h['at']:6.0f}s  {h['text']}")
    say("\n모의 훈련 통과")
    if hold:
        hold(c, clock, ctx, j)
    return ctx


if __name__ == "__main__":
    sys.exit(0 if main() else 1)
