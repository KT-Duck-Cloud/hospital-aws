from .replica import q


def accounts(ex, host, users, lock, step, timeout=30):
    """앱 계정만 잠금(+ 붙어 있는 세션 KILL) / 해제. 서버 전체를 read_only 로 만들지 않는다 — 같은 서버의 다른 스키마는 계속 쓴다."""
    names = ", ".join(q(u) for u in users)
    rows = [r.split("\t") for r in ex.sql(host, f"SELECT user, host FROM mysql.user WHERE user IN ({names});", timeout).splitlines()
            if r.strip()]
    if not rows:
        step(f"   {host} 앱 계정 없음 — 건너뜀")
        return 0
    word = "LOCK" if lock else "UNLOCK"
    ex.sql(host, "".join(f"ALTER USER {q(u)}@{q(h)} ACCOUNT {word};\n" for u, h in rows), timeout)
    killed = 0
    if lock:
        for i in ex.sql(host, f"SELECT id FROM information_schema.PROCESSLIST WHERE user IN ({names});", timeout).split():
            try:
                ex.sql(host, f"KILL {int(i)};", timeout)
                killed += 1
            except (RuntimeError, ValueError):
                pass
    step(f"   {host} 앱 계정 {word} {len(rows)}개" + (f" · 세션 {killed}개 끊음" if lock else ""))
    return len(rows)


def site(ex, hosts, users, step, timeout=20):
    """닿는 곳만 잠근다. 못 닿은 곳은 복귀 때 백업 후 다시 채우므로 승격을 막지 않는다."""
    done = []
    for h in hosts:
        try:
            accounts(ex, h, users, True, step, timeout)
            done.append(h)
        except Exception as e:
            step(f"   {h} 잠금 실패 (닿지 않음: {str(e)[:80]})")
    return done
