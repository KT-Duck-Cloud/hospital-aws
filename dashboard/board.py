"""DR 현황 화면(dr.html)이 4초마다 읽는 /api/drboard — DR 컨트롤 상태 + 실제 응답을 운영 화면과 같은 모양으로 바꾼다.
사이트 키는 화면과 같이 t3(1조 주 운영) · t5(1차 DR) · aws(2차 DR). 1조 DB 순서 = 설정 DR_SITE1_DBS 순서."""
import re
import time

PHASE_NEXT = {"NORMAL": "t3 장애 → t5 로 1차 전환", "DR1": "t5 장애 → AWS 2차 DR", "DR2": "t3 정상화 → 복귀 대기",
              "RECOVERING": "응답 유지 → AWS 데이터로 맞춘 뒤 t3 복귀", "RECOVERED": "t3 장애 → t5 로 1차 전환"}
RECOVERED_SEC = 1800


def _ago(sec):
    if sec is None:
        return "-"
    sec = max(0, int(sec))
    return f"{sec}초 전" if sec < 60 else f"{sec // 60}분 전" if sec < 3600 else f"{sec // 3600}시간 전"


def _ts(t, fmt):
    return time.strftime(fmt, time.localtime(t)) if t is not None else "-"


def _primary(door):
    m = re.search(r"server (\d+\.\d+\.\d+\.\d+):\d+ max_fails", door or "")
    return m.group(1) if m else None


def build(dr, probes):
    """dr = DR 컨트롤 /api/state, probes = [{site, kind, host, ok}]"""
    if not dr:
        return None
    s, d, now = dr["sites"], dr["dr"], dr["at"]
    dbs, apps = list(s["site1_dbs"]) + [None, None], list(s["site1_apps"]) + [None, None]
    key = {dbs[0]: "t3", dbs[1]: "t5", s["standby_db"]: "aws", apps[0]: "t3", apps[1]: "t5", s["standby_app"]: "aws"}
    ok = {(p["kind"], key.get(p["host"])): p["ok"] for p in probes}
    roles = {key.get(r["host"]): r for r in dr.get("site1") or []}
    active, job, hist = d.get("active"), dr.get("job") or {}, dr.get("history") or []
    writers = [k for k in ("t3", "t5") if k in roles and not roles[k]["ro"] and k not in [key.get(h) for h in d.get("fenced") or []]]
    act = "aws" if active else (writers[0] if writers else "t3")
    back = next((h for h in hist if "(복귀)" in h["text"]), None)

    if active:
        phase = "RECOVERING" if d.get("back_to") else "DR2"
    elif act == "t5":
        phase = "DR1"
    elif back and now - back["at"] < RECOVERED_SEC:
        phase = "RECOVERED"
    else:
        phase = "NORMAL"
    start = {"DR2": (active or {}).get("since"), "RECOVERING": d.get("up_since"), "RECOVERED": back and back["at"]}.get(phase)

    up = key.get(_primary(dr.get("frontdoor")))
    fenced = {key.get(h) for h in d.get("fenced") or []}
    sites = {}
    for k, name, role in (("t3", "온프레미스 t3", "주 운영"), ("t5", "온프레미스 t5", "1차 DR"), ("aws", "AWS t8", "2차 DR")):
        r = roles.get(k) or {}
        rp = (dr.get("repl") if k == "aws" else r.get("repl")) or {}
        repl = ({"src": key.get(rp.get("source"), rp.get("source")), "io": rp.get("io") == "Yes", "sql": rp.get("sql") == "Yes",
                 "lag": rp.get("lag"), "from": "판정 서버 확인"} if rp.get("configured") else None)
        web, db = bool(ok.get(("app", k))), bool(ok.get(("db", k)))
        writer = k == act
        state = ("on" if writer and db else "off" if not db else "warn" if k in fenced else "sb")
        word = {"on": "운영 중 (정상)", "off": "중단 · 응답 없음", "warn": "잠금 · 복귀 대기",
                "sb": "대기 · Pilot Light (정상)" if k == "aws" else "대기 · 복제 중"}[state]
        sites[k] = {"key": k, "name": name, "role": role, "state": state, "word": word, "web": web, "db": db, "node": db or web,
                    "sim_down": False, "healthy": db, "writer": writer, "sro": None if k == "aws" else float(bool(r.get("ro"))),
                    "repl": repl, "app": "active" if writer else ("off" if k == "aws" else "warm" if web else "off"),
                    "score": None, "down_for": None, "up_for": None}

    alive = d.get("tick_at") and now - d["tick_at"] < 90
    rule = dr.get("rules") or {}
    S = sites
    repl_ok = lambda k, src: bool(S[k]["repl"] and S[k]["repl"]["src"] == src and S[k]["repl"]["io"] and S[k]["repl"]["sql"])  # noqa: E731
    io = lambda k: (f"원본 {S[k]['repl']['src']} · IO {'ON' if S[k]['repl']['io'] else 'OFF'} · SQL {'ON' if S[k]['repl']['sql'] else 'OFF'}"  # noqa: E731
                    if S[k]["repl"] else "복제 정보 없음")
    engine = {"t": "DR 판정 엔진 동작 (최근 1분)", "ok": bool(alive), "d": f"마지막 판정 {_ago(now - d['tick_at']) if d.get('tick_at') else '-'}"}
    if phase in ("NORMAL", "RECOVERED"):
        expect = [{"t": "t3 운영 · 웹과 DB 응답", "ok": S["t3"]["web"] and S["t3"]["db"], "d": f"운영 사이트 = {act}"},
                  {"t": "공개 입구 → t3", "ok": up == "t3", "d": f"지금 입구: {up or '-'}"},
                  {"t": "t5 대기 · t3 를 따라 복제", "ok": repl_ok("t5", "t3"), "d": io("t5")},
                  {"t": "AWS Pilot Light 정상 (DB 상시 동기화 · App 절전 대기)", "ok": S["aws"]["repl"] is not None and S["aws"]["repl"]["io"]
                   and not S["aws"]["web"], "d": io("aws") + " · vm2 상시 가동 · vm1 절전"}, engine]
    elif phase == "DR1":
        expect = [{"t": "t5 운영 · 웹과 DB 응답", "ok": S["t5"]["web"] and S["t5"]["db"], "d": "1조 안 승격"},
                  {"t": "공개 입구 → t5", "ok": up == "t5", "d": f"지금 입구: {up or '-'}"},
                  {"t": "AWS 가 t5 를 따라 복제", "ok": repl_ok("aws", "t5"), "d": io("aws")},
                  {"t": "t3 중단 확인", "ok": not S["t3"]["db"], "d": "t3 DB " + ("응답" if S["t3"]["db"] else "응답 없음")}, engine]
    elif phase == "DR2":
        expect = [{"t": "AWS 운영 · 웹과 DB 응답", "ok": S["aws"]["web"] and S["aws"]["db"], "d": "AWS 승격 · 앱 " + str((active or {}).get("app"))},
                  {"t": "공개 입구 → AWS", "ok": up == "aws", "d": f"지금 입구: {up or '-'}"},
                  {"t": "1조 DB 쓰기 없음 (중단 또는 잠금)", "ok": all(not S[k]["db"] or k in fenced for k in ("t3", "t5")),
                   "d": " · ".join(f"{k} {'잠금' if k in fenced else '응답 없음' if not S[k]['db'] else '열림'}" for k in ("t3", "t5"))}, engine]
    else:
        upf = now - d["up_since"] if d.get("up_since") else 0
        expect = [{"t": "AWS 무중단 운영 유지", "ok": S["aws"]["web"] and S["aws"]["db"], "d": "공개 입구 = AWS"},
                  {"t": "t3 응답 · 앱 계정 잠금", "ok": S["t3"]["db"] and "t3" in fenced, "d": "옛 쓰기 DB 로 기록이 들어가지 않게"},
                  {"t": f"복귀 조건 {rule.get('failback_sec', 180)}초 연속 응답", "ok": None if upf < rule.get("failback_sec", 180) else True,
                   "d": f"{int(upf)}초 / {rule.get('failback_sec', 180)}초"}, engine]

    note, wit = d.get("note") or "", d.get("witness") or {}
    down = now - d["down_since"] if d.get("down_since") else 0
    cand = [k for k in ("t3", "t5", "aws") if k != act and S[k]["db"] and S[k]["state"] != "warn"]
    rto = next((re.search(r"RTO (\d+)초", h["text"]).group(1) for h in hist if "RTO" in h["text"]), None)
    flow = [
        {"k": "detect", "t": "감지", "who": "판정 서버 · 독립 프로브", "st": "bad" if down else "ok",
         "main": f"1조 DB 무응답 {int(down)}초" if down else f"운영 {act} 정상",
         "sub": " · ".join(f"{k} DB {'○' if S[k]['db'] else '✕'}" for k in ("t3", "t5", "aws"))},
        {"k": "judge", "t": "판정", "who": f"DR 엔진 · {'자동' if dr.get('mode') == 'auto' else '수동'} 모드",
         "st": "warn" if wit.get("veto") and down else "bad" if down else "ok",
         "main": ("증인 보류" if wit.get("veto") and down else f"승격 대기 {int(down)}/{rule.get('failover_sec', 300)}초" if down and not active
                  else "AWS 대행 중" if active else "조치 필요 없음"), "sub": note},
        {"k": "pick", "t": "대상 선정", "who": "우선순위 t3 → t5 → AWS", "st": "ok" if cand else "warn",
         "main": f"다음 후보 {cand[0] if cand else '없음'}", "sub": f"운영 {act} · 정상 후보: {', '.join(cand) or '-'}"},
        {"k": "link", "t": "실행 연계", "who": "DB · 공개 입구 · 복제", "st": "ok" if up == act else "warn",
         "main": f"DB 쓰기 {act} · 입구 {up or '-'}",
         "sub": f"앱 {' · '.join(k + ' ' + S[k]['app'] for k in ('t3', 't5', 'aws'))} · 복제 "
                + " · ".join(f"{S[k]['repl']['src']}→{k}" for k in ("t5", "aws") if S[k]["repl"])},
        {"k": "verify", "t": "검증", "who": "공개 서비스 · RTO", "st": "ok" if S[act]["web"] else "bad",
         "main": "병원 서비스 응답 정상" if S[act]["web"] else "병원 서비스 응답 없음",
         "sub": (f"최근 전환 판정 RTO {rto}초" if rto else "전환 기록 없음") + " · RPO 0초 목표"}]

    def lv(t):
        return ("FAIL" if "실패" in t else "CRITICAL" if "승격 시작" in t or "복귀 시작" in t
                else "SUCCESS" if any(w in t for w in ("완료", "(자동 승격)", "(복귀)")) else "WARN" if "잠금" in t else "INFO")
    events = [{"t": _ts(h["at"], "%m-%d %H:%M:%S"), "lv": lv(h["text"]), "stage": "", "site": act, "title": h["text"],
               "rto": int(re.search(r"RTO (\d+)초", h["text"]).group(1)) if "RTO" in h["text"] else 0} for h in hist[:20]]
    return {"at": now, "at_s": _ts(now, "%H:%M:%S"), "phase": phase, "act": act, "mode": "auto" if dr.get("mode") == "auto" else "manual",
            "busy": bool(job.get("running")), "demo_step": None, "stage": f"STAGE_{phase}",
            "since": _ago(now - start) if start else "-", "last_why": hist[0]["text"] if hist else "", "next": PHASE_NEXT[phase],
            "sites": sites, "expect": expect, "flow": flow, "agree": {"orch": act, "upstream": up, "hsagent": None},
            "events": events, "upstream": up, "src": "판정 서버 · DR 컨트롤 상태"}
