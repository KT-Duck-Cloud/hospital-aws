"""관리 사이트 DR 칸 (GET /dr/api) — DR 컨트롤 상태를 '승격 조건 하나씩' 으로 풀어 보여 준다. 조회만.
조건: 1조 DB 응답 · 대기 준비 · 복제 IO/SQL · 지연 ≤ max_lag · 자동 모드 · 증인. 다섯 개가 모두 맞아야 장애 때 자동 승격."""


def dr_api(st):
    """st = Collector.status() → admin_dr 화면 모양"""
    dr = (st or {}).get("dr")
    if not dr:
        return {"ok": False, "error": " · ".join((st or {}).get("errors") or ["DR 컨트롤 응답 없음"])}
    now, d, rules, repl = dr["at"], dr["dr"], dr.get("rules") or {}, dr.get("repl") or {}
    probes = {(p["kind"], p["host"]): p["ok"] for p in st.get("probes") or []}
    s, fo, fb, lag_max = dr["sites"], rules.get("failover_sec", 300), rules.get("failback_sec", 180), rules.get("max_lag_sec", 60)
    s1_db = any(probes.get(("db", h)) for h in s["site1_dbs"])
    down = int(now - d["down_since"]) if d.get("down_since") else 0
    lag, auto, wit = repl.get("lag"), dr.get("mode") == "auto", d.get("witness") or {}
    crit = [
        {"k": "1조 DB 응답", "v": "응답" if s1_db else f"무응답 {down}초", "need": f"{fo}초 연속 무응답이면 승격 시작",
         "ok": None if s1_db else down >= fo, "trigger": True},
        {"k": "대기 DB 응답", "v": "응답" if probes.get(("db", s["standby_db"])) else "응답 없음", "need": "AWS vm2 MySQL 3306",
         "ok": bool(probes.get(("db", s["standby_db"])))},
        {"k": "역할 준비", "v": "완료" if dr.get("prepared") else "필요", "need": "복제 연결 · 앱 계정 (prepare)", "ok": bool(dr.get("prepared"))},
        {"k": "복제 IO · SQL", "v": f"IO {repl.get('io', '-')} · SQL {repl.get('sql', '-')}" if repl.get("configured") else "없음",
         "need": "둘 다 Yes", "ok": repl.get("io") == "Yes" and repl.get("sql") == "Yes"},
        {"k": "복제 지연", "v": f"{lag}초" if lag is not None else "-", "need": f"≤ {lag_max}초 (장애 직전 기준)",
         "ok": lag is not None and lag <= lag_max},
        {"k": "자동 승격", "v": "켜짐" if auto else "꺼짐", "need": "mode auto", "ok": auto},
        {"k": "증인", "v": wit.get("veto") or "통과" if wit else "장애 때 확인", "need": "AWS 가 1조 DB 를 더 이상 못 받음 (IO No)",
         "ok": None if not wit else not wit.get("veto")}]
    ready_n = sum(1 for c in crit[1:6] if c["ok"])
    if d.get("active"):
        phase, tone = f"발동 — AWS 운영 중 · 1조 DB {fb}초 응답 시 자동 복귀", "warn"
    elif not s1_db:
        phase, tone = f"감시 — 1조 DB 무응답 {down}/{fo}초" + ("" if ready_n == 5 else " · 대기 준비 안 됨 (승격 불가)"), "bad"
    else:
        phase, tone = ("대기 — 1조 정상 · AWS 준비 완료 (장애 시 자동 승격)", "ok") if ready_n == 5 else (f"대기 — 1조 정상 · 준비 {ready_n}/5", "warn")
    vm = lambda h: {"name": h, "ip": h, "online": bool(probes.get(("db", h)) or probes.get(("app", h)))}  # noqa: E731
    job = dr.get("job") or {}
    return {"ok": True, "at": now, "phase": phase, "tone": tone, "ready": ready_n == 5, "ready_n": ready_n, "active": d.get("active"),
            "one": {"member": "t3 · t5", "db": s["site1_dbs"][0] if s["site1_dbs"] else None, "db_ok": s1_db,
                    "vms": [vm(h) for h in s["site1_apps"] + s["site1_dbs"]]},
            "two": {"member": "t8", "state": "운영" if d.get("active") else "대기", "ready": "완료" if dr.get("prepared") else "필요",
                    "prepared": bool(dr.get("prepared")), "repl": repl, "score": None, "vms": [vm(s["standby_app"]), vm(s["standby_db"])],
                    "link": f"{repl.get('source') or '-'} → {s['standby_db']}"},
            "crit": crit, "rules": {"failover_min": fo // 60, "failback_min": fb // 60, "stable_min": 0, "lag_max": lag_max},
            "dr": {"down_min": down // 60, "fenced": d.get("fenced"), "note": d.get("note"), "failover_try": d.get("failover_try"), "witness": wit or None},
            "job": {"running": job.get("running"), "kind": job.get("name"), "step": (job.get("steps") or [[0, ""]])[-1][1], "error": None},
            "names": {}, "history": [{"at": h["at"], "text": h["text"]} for h in (dr.get("history") or [])[:10]], "controller": "DR 컨트롤",
            "preflight": {}}
