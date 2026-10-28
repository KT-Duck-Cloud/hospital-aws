from dataclasses import dataclass

from . import fence, power, replica
from .replica import q


@dataclass
class Ctx:
    cfg: object
    clock: object
    probe: object
    ex: object
    power: object
    door: object
    store: object
    secrets: object
    runner: object

    def say(self, text):
        self.store.log(self.clock.now(), text)


def site1_roles(ctx):
    out = []
    for h in ctx.cfg.site1_dbs:
        if ctx.probe.tcp_ok(h, 3306):
            try:
                out.append(replica.role(ctx.ex, h))
            except Exception:
                pass
    return out


def door(ctx):
    c = ctx.cfg
    if ctx.store.dr["active"]:
        primary, backup = [c.standby_app], []
    else:
        up = [a for a in c.site1_apps if ctx.probe.app_ready(a, c.app_port, c.ready_path)]
        primary, backup = up[:1], up[1:]
    try:
        if ctx.door.point(primary, backup):
            ctx.say(f"공개 입구 → {', '.join(primary) or '안내 페이지'}" + (f" (backup {', '.join(backup)})" if backup else ""))
    except Exception as e:
        ctx.say(f"공개 입구 전환 실패 — {str(e)[:120]}")


def prepare(ctx, src=None, force=False):
    c, now = ctx.cfg, ctx.clock.now()
    if ctx.store.dr["active"]:
        raise ValueError("AWS 가 1조를 대행 중 — 복귀 뒤에")
    src = src or replica.choose_source(site1_roles(ctx))[0]
    if not src:
        raise ValueError("1조 DB 가 응답하지 않음")
    ctx.say(f"대기 준비 시작 — {src} → {c.standby_db}" + (" (다시 채움)" if force else ""))

    def run(step):
        return replica.prepare(ctx.ex, ctx.clock, c, src, ctx.secrets.get(c.repl_user), step, force)

    def done(_n, ok, st):
        ctx.store.data["prepared"] = {"at": now, "ok": ok, "source": src, "reseeded": st.get("reseeded"), "error": st.get("error", "")}
        ctx.say(f"대기 준비 {'완료' if ok else '실패'} — {src} → {c.standby_db}" + ("" if ok else f" ({st.get('error', '')[:80]})"))
        ctx.store.save()

    return ctx.runner.start("prepare", run, done)


def follow(ctx, st):
    """AWS 복제 원본 유지. 옮기는 경우는 둘 — 원본이 죽어 IO 가 끊겼을 때, 또는 원본이 쓰기 DB 인데 다 따라온 대기 DB 가 있을 때."""
    c = ctx.cfg
    if ctx.store.dr["active"] or ctx.runner.busy or not st.get("configured"):
        return
    roles = site1_roles(ctx)
    want, why = replica.choose_source(roles)
    cur = st.get("source")
    if not want or want == cur:
        return
    now_role = next((r for r in roles if r["host"] == cur), None)
    if replica.running(st) and (now_role is None or now_role["ro"] or not replica.caught_up(ctx.ex, want, cur)):
        return
    replica.grant(ctx.ex, want, c.repl_user, c.standby_db, ctx.secrets.get(c.repl_user))
    ctx.ex.sql(c.standby_db, f"STOP REPLICA;\nCHANGE REPLICATION SOURCE TO SOURCE_HOST={q(want)};\nSTART REPLICA;\n", 30)
    ctx.say(f"AWS 복제 원본 {cur} → {want} ({why})")


def failover(ctx):
    c, d = ctx.cfg, ctx.store.dr
    ctx.say(f"승격 시작 — 1조 DB {c.failover_sec // 60}분 무응답 → AWS {c.standby_db}")

    def run(step):
        fenced = fence.site(ctx.ex, c.site1_dbs, c.app_users, step)
        st = replica.promote(ctx.ex, ctx.clock, c.standby_db, step)
        try:
            st.update(power.app_up(ctx, step))
        except Exception as e:
            step(f"   앱 기동 실패: {str(e)[:160]} — {c.retry_sec // 60}분 뒤 다시")
            st["app_error"] = str(e)[:160]
        return {**st, "fenced": fenced}

    def done(_n, ok, st):
        now = ctx.clock.now()
        if not ok:
            ctx.say(f"승격 실패 ({st.get('error', '')[:80]}) — {c.retry_sec // 60}분 뒤 다시")
            ctx.store.save()
            return
        d.update(active={"since": now, "gtid": st["gtid"], "app": st.get("app") or "error"}, fenced=st["fenced"],
                 app_try=now if st.get("app_error") else 0, back_to=None, up_since=None)
        ctx.store.data.update(prepared=None, repl_ok=None)
        door(ctx)
        ctx.say(f"1조 = AWS {c.standby_db} (자동 승격) · RTO {now - d['down_since']:.0f}초"
                + ("" if st["fenced"] else " · 1조 잠금 못 함 — 돌아오는 즉시 잠그고, 복귀 때 백업 후 다시 채움"))
        ctx.store.save()

    return ctx.runner.start("failover", run, done)


def app_retry(ctx):
    def done(_n, ok, st):
        if ok and ctx.store.dr["active"]:
            ctx.store.dr["active"]["app"] = "up"
        ctx.say(f"AWS 앱 다시 기동 {'완료' if ok else '실패 (' + st.get('error', '')[:80] + ')'}")
        ctx.store.save()
    try:
        ctx.runner.start("app", lambda step: power.app_up(ctx, step), done)
    except ValueError:
        pass


def failback(ctx, back):
    c, d = ctx.cfg, ctx.store.dr
    ctx.say(f"복귀 시작 — {back} 를 AWS {c.standby_db} 데이터로 맞춘 뒤 전환")

    def run(step):
        lock = lambda h, on: fence.accounts(ctx.ex, h, c.app_users, on, step)   # noqa: E731
        sync = replica.failback_sync(ctx.ex, ctx.clock, c, c.standby_db, back, ctx.secrets.get(c.repl_user + "_fb"), step, lock)
        sw = replica.switchover(ctx.ex, c, c.standby_db, back, step, lock)
        try:
            power.app_down(ctx, step)
        except Exception as e:
            step(f"   앱 정지 실패: {str(e)[:160]} (DB 는 읽기 전용)")
        return {"backup": sync["backup"], **sw}

    def done(_n, ok, st):
        if not ok:
            ctx.say(f"복귀 실패 — 1조는 그대로 AWS ({st.get('error', '')[:80]}) · {c.retry_sec // 60}분 뒤 다시")
            ctx.store.save()
            return
        rest = [h for h in d["fenced"] if h != back]
        d.update(active=None, back_to=None, up_since=None, down_since=None, fenced=rest, last_backup=st["backup"], writer=back)
        door(ctx)
        ctx.say(f"1조 = {back} (복귀) · 백업 {st['backup']}" + (f" · {', '.join(rest)} 잠금 유지 — 1조 내부 복제를 다시 맞춘 뒤 해제" if rest else ""))
        ctx.store.save()
        prepare(ctx, src=back, force=True)

    return ctx.runner.start("failback", run, done)
