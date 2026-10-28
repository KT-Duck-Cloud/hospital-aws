"""시연 시나리오 ①~④ · 리셋 — DR 현황 화면의 버튼과 모의 훈련이 같이 쓴다. 가짜 인프라에 장애를 내고 판정이 끝날 때까지 시간을 흘린다.

  ① t3 다운 → t5      1조 안 전환은 1조 담당 영역 — 여기서는 가짜 인프라가 대신 t5 를 쓰기로 올린다. AWS 는 원본을 t5 로 옮겨 따라간다 (follow)
  ② t5 다운 → AWS     1조 DB 가 모두 무응답 → 판정이 증인 확인 후 AWS 승격
  ③ t3 복구           돌아온 t3 를 즉시 잠그고 복귀 대기
  ④ t3 주 서버 복귀    역동기화 → 절체 → AWS 앱 정지 → AWS 다시 대기
  리셋                t5 를 t3 기준으로 다시 맞추고 잠금 해제 (1조 담당 영역 — 가짜 인프라가 대신)
"""
from sim.cluster import Chan

T3, T5, AWS_DB, AWS_APP = "100.64.0.32", "100.64.0.52", "100.64.0.82", "100.64.0.81"
T3_APP, T5_APP = "100.64.0.31", "100.64.0.51"
NEXT = {"NORMAL": 1, "RECOVERED": 1, "DR1": 2, "DR2": 3, "RECOVERING": 4}


def ticks(j, clock, ctx, until, limit_min=15, say=None):
    last = None
    for _ in range(int(limit_min * 60 // ctx.cfg.interval_sec)):
        note = j.tick()
        if say and note != last:
            say(f"  t+{clock.now():6.0f}s  {note}")
            last = note
        if until():
            return True
        clock.sleep(ctx.cfg.interval_sec)
    return until()


def site1_switch(c, to):
    """1조 담당이 하는 1조 안 승격을 가짜로 — 대기 DB 를 쓰기로."""
    s = c.servers[to]
    s.chans.clear()
    s.ro = s.sro = False
    for k in list(s.users):
        if k[0].startswith("app_"):
            s.users[k] = False


def down(c, db, app):
    c.servers[db].up = False
    c.apps[app] = False


def up(c, db, app):
    c.servers[db].up = True
    c.apps[app] = True


def scenario(n, c, clock, ctx, j, say=None):
    d = ctx.store.dr
    if n == 1:
        down(c, T3, T3_APP)
        site1_switch(c, T5)
        ok = ticks(j, clock, ctx, lambda: (ctx.store.data["repl"] or {}).get("source") == T5, 5, say)
        return ok, "t3 다운 · t5 쓰기 승격 · AWS 복제 원본 → t5" if ok else "AWS 가 t5 로 옮기지 못함"
    if n == 2:
        down(c, T5, T5_APP)
        ok = ticks(j, clock, ctx, lambda: bool(d["active"]) and not ctx.runner.busy, 15, say)
        return ok, f"AWS 승격 · 입구 → {ctx.cfg.standby_app}" if ok else "AWS 승격 안 됨 — 판정 보류 사유: " + d["note"]
    if n == 3:
        up(c, T3, T3_APP)
        ok = ticks(j, clock, ctx, lambda: T3 in d["fenced"] and d["back_to"] == T3, 3, say)
        return ok, "t3 응답 · 앱 계정 잠금 · 복귀 대기" if ok else "t3 잠금 안 됨"
    if n == 4:
        ok = ticks(j, clock, ctx, lambda: not d["active"] and not ctx.runner.busy and (ctx.store.data["prepared"] or {}).get("ok"), 10, say)
        return ok, "t3 복귀 · AWS 다시 대기" if ok else "복귀 안 됨 — " + d["note"]
    raise ValueError(n)


def reset(c, clock, ctx, j):
    """t5 를 t3 의 복제본으로 되돌리고, 남은 잠금을 푼다."""
    t3, t5 = c.servers[T3], c.servers[T5]
    up(c, T5, T5_APP)
    t5.gtids = {u: set(v) for u, v in t3.gtids.items()}
    t5.rows, t5.binlog, t5.purged = list(t3.rows), list(t3.binlog), {}
    t5.ro = t5.sro = True
    t5.chans.clear()
    ch = t5.chans.setdefault("", Chan())
    ch.source, ch.user, ch.io, ch.sql = T3, "repl_t5", True, True
    t3.users[("repl_t5", T5)] = False
    for k in list(t5.users):
        if k[0].startswith("app_"):
            t5.users[k] = False
    ctx.store.dr["fenced"] = [h for h in ctx.store.dr["fenced"] if h != T5]
    ticks(j, clock, ctx, lambda: True, 1)
    return True, "리셋 — t3 운영 · t5 · AWS 는 t3 를 따라 복제"
