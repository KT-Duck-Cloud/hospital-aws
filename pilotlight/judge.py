"""판정 — 30초마다. 1조 DB 가 모두 failover_sec 동안 무응답 + 대기 정상 + 증인 통과일 때만 AWS 승격."""
from . import fence, flows, replica

CONN_ERRS = {2003, 2005, 2006, 2013, 1158, 1159, 1160, 1161}
PRE_DOWN_SEC = 300


class Judge:
    def __init__(self, ctx):
        self.ctx = ctx

    @property
    def auto(self):
        return (self.ctx.store.data.get("mode") or self.ctx.cfg.mode) == "auto"

    def tick(self):
        ctx, c = self.ctx, self.ctx.cfg
        now, d = ctx.clock.now(), ctx.store.dr
        d["tick_at"] = now
        self.poll(now)
        alive = [h for h in c.site1_dbs if ctx.probe.tcp_ok(h, 3306)]
        ctx.store.data["site1"] = [{"host": r["host"], "ro": r["ro"], "repl": r["repl"]} for r in flows.site1_roles(ctx)] if alive else []
        if d["active"]:
            self.fence_returning(alive)
            back = alive[0] if alive else None
            if back != d["back_to"]:
                d["up_since"] = None
            d["back_to"] = back
        if alive:
            d["up_since"], d["down_since"] = d["up_since"] or now, None
        else:
            d["down_since"], d["up_since"] = d["down_since"] or now, None
        (self.active if d["active"] else self.standby)(now, d, alive)
        ctx.store.save()
        return d["note"]

    def poll(self, now):
        ctx, c, data = self.ctx, self.ctx.cfg, self.ctx.store.data
        try:
            st = data["repl"] = replica.status(ctx.ex, c.standby_db)
        except Exception as e:
            data["repl"] = {"error": f"확인 실패: {str(e)[:120]}"}
            return
        if not (data["prepared"] or {}).get("ok"):
            if not ctx.store.dr["active"] and replica.running(st) and st.get("source") in c.site1_dbs:
                data["prepared"] = {"at": now, "ok": True, "source": st["source"], "auto": True}
                ctx.say(f"AWS 가 이미 {st['source']} 에서 복제 중 — 대기 준비 완료로 편입")
            return
        if replica.running(st):
            data["repl_ok"] = {"at": now, "lag": st.get("lag")}
        try:
            flows.follow(ctx, st)
        except Exception as e:
            ctx.say(f"AWS 복제 원본 옮기기 실패 — {str(e)[:120]}")

    def not_ready(self, d):
        ctx, c, data = self.ctx, self.ctx.cfg, self.ctx.store.data
        ok = data["repl_ok"]
        return ("AWS 대기 준비 안 됨" if not (data["prepared"] or {}).get("ok") else
                "AWS DB 응답 없음" if not ctx.probe.tcp_ok(c.standby_db, 3306) else
                "장애 직전 복제가 정상이 아니었음" if not ok or ok["at"] < d["down_since"] - PRE_DOWN_SEC
                or (ok.get("lag") or 0) > c.max_lag_sec else "")

    def witness(self):
        """증인 = AWS 복제본의 IO 스레드. AWS 는 1조 DB 에 따로 붙어 있으므로, 아직 받고 있으면 1조는 살아 있고
        판정 서버의 경로만 끊긴 것 — 이때 승격하면 쓰기 DB 가 둘이 된다(split-brain)."""
        try:
            st = replica.status(self.ctx.ex, self.ctx.cfg.standby_db)
        except Exception as e:
            return f"증인 확인 실패 ({str(e)[:80]}) — 보류"
        if st.get("io") == "Yes":
            return f"증인: AWS 가 {st.get('source')} 를 아직 받는 중 (IO Yes) — 판정 쪽 경로 문제로 보고 보류"
        if st.get("errno") not in CONN_ERRS:
            return f"증인: IO {st.get('io')} · 오류 {st.get('errno')} — 접속 끊김이 아님 (수동 정지 · 인증), 관리자 확인"
        return ""

    def standby(self, now, d, alive):
        ctx, c = self.ctx, self.ctx.cfg
        if alive:
            d["note"] = f"1조 정상 — DB {len(alive)}/{len(c.site1_dbs)} 응답 · AWS 대기"
            if not ctx.runner.busy:
                flows.door(ctx)
            return
        down = now - d["down_since"]
        why = self.not_ready(d)
        if not why and down >= c.failover_sec:
            why = self.witness()
            d["witness"] = {"at": now, "veto": why}
        d["note"] = f"1조 DB 모두 응답 없음 {int(down // 60)}분" + (
            f" — 승격 불가: {why}" if why else " — AWS 승격" if down >= c.failover_sec else f" — {c.failover_sec // 60}분이면 AWS 승격")
        if down >= c.failover_sec and not why and self.auto and not ctx.runner.busy and now - d["failover_try"] >= c.retry_sec:
            d["failover_try"] = now
            flows.failover(ctx)

    def active(self, now, d, alive):
        ctx, c = self.ctx, self.ctx.cfg
        busy = ctx.runner.busy
        if not busy and now - d["app_try"] >= c.retry_sec and not ctx.probe.app_ready(c.standby_app, c.app_port, c.ready_path):
            d["app_try"] = now
            flows.app_retry(ctx)
        back, up = d["back_to"], now - d["up_since"] if d["up_since"] else 0
        if not back:
            d["note"] = "1조 = AWS (승격) · 1조 DB 아직 응답 없음"
            return
        if not ctx.probe.tcp_ok(c.standby_db, 3306):
            d["note"] = f"1조 {back} 응답 {int(up // 60)}분 · AWS DB 비정상 — 자동 복귀 안 함 (관리자 확인)"
            return
        d["note"] = f"1조 {back} 응답 {int(up // 60)}분 — {c.failback_sec // 60}분이면 AWS 데이터로 맞춘 뒤 복귀"
        if up >= c.failback_sec and self.auto and not busy and now - d["failback_try"] >= c.retry_sec:
            d["failback_try"] = now
            flows.failback(ctx, back)

    def fence_returning(self, alive):
        """AWS 가 1조인 동안 돌아온 1조 DB 는 즉시 앱 계정부터 잠근다 — 옛 쓰기 DB 로 새 기록이 들어가지 않게."""
        ctx, d = self.ctx, self.ctx.store.dr
        for h in alive:
            if h in d["fenced"] or ctx.runner.busy:
                continue
            try:
                fence.accounts(ctx.ex, h, ctx.cfg.app_users, True, lambda m: None, timeout=15)
                d["fenced"].append(h)
                ctx.say(f"1조 {h} DB 응답 — 복귀 전까지 앱 계정 잠금")
            except Exception as e:
                d["note"] = f"{h} 앱 계정 잠금 실패: {str(e)[:100]}"
