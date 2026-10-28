"""MySQL 8.4 GTID 복제 — 대기 준비(prepare) · 승격(promote) · 역동기화(failback_sync) · 절체(switchover)."""
import os
import re
import time

HERE = os.path.dirname(os.path.abspath(__file__))
FB_CH = "failback"


def q(s):
    return "'" + str(s).replace("\\", "\\\\").replace("'", "''") + "'"


def gtid(s):
    return re.sub(r"(\\n|\s)+", "", s or "")


def _block(out, key):
    m = re.search(rf"^[ \t]*{key}: (.*?)(?=^[ \t]*[A-Za-z_]+:(?: |$)|\Z)", out, re.M | re.S)
    return m.group(1) if m else ""


def parse_status(out):
    kv = dict(re.findall(r"^[ \t]*(\w+): ?(.*)$", out, re.M))
    if not kv:
        return {"configured": False}
    lag, errno = kv.get("Seconds_Behind_Source"), kv.get("Last_IO_Errno") or "0"
    return {"configured": True, "source": kv.get("Source_Host"), "io": kv.get("Replica_IO_Running"),
            "sql": kv.get("Replica_SQL_Running"), "lag": int(lag) if lag and lag.isdigit() else None,
            "retrieved": gtid(_block(out, "Retrieved_Gtid_Set")), "errno": int(errno) if errno.isdigit() else 0,
            "error": (kv.get("Last_IO_Error") or kv.get("Last_SQL_Error") or "")[:200]}


def status(ex, host, channel=None):
    ch = f" FOR CHANNEL {q(channel)}" if channel else ""
    return parse_status(ex.run(host, f"sudo -n mysql -e \"SHOW REPLICA STATUS{ch}\\G\"", b"", 30).decode())


def running(st):
    return bool(st) and st.get("io") == "Yes" and st.get("sql") == "Yes"


def wait(ex, clock, host, ok=lambda s: running(s) and s.get("lag") is not None, tries=20, every=3, channel=None):
    st = {}
    for _ in range(tries):
        clock.sleep(every)
        st = status(ex, host, channel)
        if ok(st):
            break
    return st


def source_sql(src, user, pw, db, channel=None, retry=86400):
    ch = f" FOR CHANNEL {q(channel)}" if channel else ""
    return (f"CHANGE REPLICATION SOURCE TO SOURCE_HOST={q(src)}, SOURCE_PORT=3306, SOURCE_USER={q(user)}, "
            f"SOURCE_PASSWORD={q(pw)}, SOURCE_AUTO_POSITION=1, SOURCE_SSL=1, GET_SOURCE_PUBLIC_KEY=1, "
            f"SOURCE_CONNECT_RETRY=10, SOURCE_RETRY_COUNT={retry}{ch};\n"
            f"CHANGE REPLICATION FILTER REPLICATE_DO_DB=({db}){ch};\n")


def grant(ex, src, user, host, pw):
    """복제 계정은 binlog 에 남기지 않는다 (sql_log_bin=0 — 하류로 번지지 않고 GTID 도 생기지 않음)."""
    acct = f"{q(user)}@{q(host)}"
    body = (f"CREATE USER IF NOT EXISTS {acct} IDENTIFIED WITH caching_sha2_password BY {q(pw)} REQUIRE SSL;\n"
            f"ALTER USER {acct} IDENTIFIED WITH caching_sha2_password BY {q(pw)} REQUIRE SSL;\n"
            f"GRANT REPLICATION SLAVE ON *.* TO {acct};\n")
    if ex.sql(src, "SELECT @@super_read_only;\n").strip() == "1":
        body = "SET GLOBAL super_read_only=OFF;\n" + body + "SET GLOBAL super_read_only=ON;\n"
    ex.sql(src, "SET SESSION sql_log_bin=0;\n" + body)


def role(ex, host):
    """(읽기 전용, 받은 것을 자기 binlog 에 남김, 자기 복제 상태)"""
    ro, ro2, chain = (ex.sql(host, "SELECT @@super_read_only, @@read_only, @@log_replica_updates;\n").split() + ["0"] * 3)[:3]
    return {"host": host, "ro": "1" in (ro, ro2), "chain": chain == "1", "repl": status(ex, host)}


def caught_up(ex, behind, ahead):
    """behind 가 ahead 의 모든 트랜잭션을 이미 가졌는가 — 사슬 원본을 바꿀 때 데이터가 빠지지 않게."""
    want = gtid(ex.sql(ahead, "SELECT @@GLOBAL.gtid_executed;\n"))
    return ex.sql(behind, f"SELECT GTID_SUBSET({q(want)}, @@GLOBAL.gtid_executed);\n").strip() == "1"


def choose_source(roles):
    """대기 DB(읽기 전용 · 사슬 · 복제 정상, 원본이 살아 있는 쪽) 우선 → 없으면 쓰기 DB → 없으면 아무거나."""
    alive = {r["host"] for r in roles}
    for r in roles:
        if r["ro"] and r["chain"] and running(r["repl"]) and r["repl"].get("source") in alive:
            return r["host"], f"대기 DB (← {r['repl']['source']})"
    w = next((r["host"] for r in roles if not r["ro"]), None)
    return (w or (roles[0]["host"] if roles else None)), "쓰기 DB"


def prepare(ex, clock, cfg, src, pw, step, force=False):
    rep, user = cfg.standby_db, cfg.repl_user
    step(f"1 접속 {rep}")
    ex.run(rep, "sudo -n true", b"", 30)
    step("2 MySQL 8.4 복제본 설치 · 설정")
    with open(os.path.join(HERE, "mysql_replica.sh")) as f:
        out = ex.script(rep, f.read(), str(cfg.standby_server_id))
    step("   " + (out.strip().splitlines() or ["?"])[-1])
    step(f"3 원본 {src} 에 복제 계정 {user}@{rep}")
    grant(ex, src, user, rep, pw)
    change = source_sql(src, user, pw, cfg.db_name)
    cur = status(ex, rep)
    if cur.get("configured") and not force:
        same = cur.get("source") == src and running(cur)
        step(f"4 {'이미 같은 원본' if same else '원본만 바꿈 ' + str(cur.get('source')) + ' → ' + src} — GTID 자동 위치로 이어 받기")
        ex.sql(rep, "STOP REPLICA;\n" + change + "START REPLICA;\n")
        st = wait(ex, clock, rep, tries=10)
        if running(st):
            return {**st, "reseeded": False}
        step(f"   이어 받기 실패 ({st.get('errno') or st.get('io')}) — 다시 채움")
    step(f"4 {cfg.db_name} 채우기 — 원본 덤프 → 복제본")
    ex.sql(rep, "STOP REPLICA;\nRESET REPLICA ALL;\nRESET PERSIST IF EXISTS super_read_only;\nRESET PERSIST IF EXISTS read_only;\n"
                "SET GLOBAL super_read_only=OFF;\nSET GLOBAL read_only=OFF;\n"
                f"SET SESSION sql_log_bin=0;\nDROP DATABASE IF EXISTS {cfg.db_name};\nRESET BINARY LOGS AND GTIDS;\n")
    data = ex.dump(src, cfg.db_name)
    step(f"   덤프 {len(data) // 1024} KB")
    ex.load(rep, data)
    step(f"5 복제 연결 {src}:3306 → {rep} (GTID · SSL · {cfg.db_name} 만)")
    ex.sql(rep, change + "SET GLOBAL read_only=ON;\nSET GLOBAL super_read_only=ON;\nSTART REPLICA;\n")
    st = wait(ex, clock, rep)
    if not running(st):
        raise RuntimeError(f"복제가 시작되지 않음: IO={st.get('io')} SQL={st.get('sql')} {st.get('error')}")
    count = f"SELECT COUNT(*) FROM information_schema.TABLES WHERE TABLE_SCHEMA={q(cfg.db_name)};"
    a, b = ex.sql(src, count).strip(), ex.sql(rep, count).strip()
    step(f"6 확인 — 지연 {st.get('lag')}초 · 테이블 {a}/{b}")
    if a != b:
        raise RuntimeError(f"테이블 수가 다름 (원본 {a}, 복제본 {b})")
    return {**st, "reseeded": True}


def promote(ex, clock, host, step):
    st = status(ex, host)
    step(f"승격 1 IO 정지 — 받아 둔 relay 적용 대기 (받은 GTID {st.get('retrieved') or '없음'})")
    ex.sql(host, "STOP REPLICA IO_THREAD;\n")
    if st.get("retrieved"):
        r = ex.sql(host, f"SELECT WAIT_FOR_EXECUTED_GTID_SET({q(st['retrieved'])}, 120);\n", 180).strip()
        if r != "0":
            raise RuntimeError(f"relay 를 120초 안에 다 적용하지 못함 ({r}) — 승격 중단")
    step("승격 2 복제 끊기 · 쓰기 가능 (SET PERSIST)")
    ex.sql(host, "STOP REPLICA;\nRESET REPLICA ALL;\nSET PERSIST super_read_only=OFF;\nSET PERSIST read_only=OFF;\n")
    ro = ex.sql(host, "SELECT CONCAT(@@read_only, @@super_read_only);\n").strip()
    if ro != "00":
        raise RuntimeError(f"쓰기 가능으로 바뀌지 않음 ({ro})")
    gt = gtid(ex.sql(host, "SELECT @@GLOBAL.gtid_executed;\n"))
    step(f"   {host} 쓰기 가능 · GTID {gt[:100]}")
    return {"ip": host, "gtid": gt}


def failback_sync(ex, clock, cfg, src, back, pw, step, lock):
    """복귀 대상(back)을 승격된 AWS(src)의 복제본으로 — back 의 다른 스키마 · GTID 는 그대로 두고 모자란 GTID 만 더한다."""
    user = cfg.repl_user + "_fb"
    step(f"복귀 1 {back} 앱 계정 잠금")
    lock(back, True)
    bk = f"{cfg.backup_dir}/{cfg.db_name}-before-failback-{time.strftime('%Y%m%d-%H%M%S', time.gmtime(clock.now()))}.sql.gz"
    step(f"복귀 2 {back} {cfg.db_name} 백업 → {bk}")
    ex.run(back, f"sudo -n sh -c 'mkdir -p {cfg.backup_dir} && chmod 700 {cfg.backup_dir} && mysqldump --single-transaction "
                 f"--set-gtid-purged=OFF --routines --triggers --events --hex-blob --default-character-set=utf8mb4 "
                 f"--databases {cfg.db_name} | gzip > {bk} && chmod 600 {bk}'", b"", 1200)
    step(f"복귀 3 {src} 에 복제 계정 {user}@{back}")
    grant(ex, src, user, back, pw)
    data = ex.dump(src, cfg.db_name).decode("utf-8", "replace")
    m = re.search(r"SET @@GLOBAL\.GTID_PURGED=(?:/\*!80000 '\+'\*/ )?'([^']*)';\n?", data, re.S)
    if not m:
        raise RuntimeError("덤프에 GTID 정보가 없음 — 중단")
    gset, data = gtid(m.group(1)), data[:m.start()] + data[m.end():]
    if ex.sql(back, f"SELECT COUNT(*) FROM performance_schema.replication_connection_configuration "
                    f"WHERE CHANNEL_NAME={q(FB_CH)};\n").strip() != "0":
        ex.sql(back, f"STOP REPLICA FOR CHANNEL {q(FB_CH)};\nRESET REPLICA ALL FOR CHANNEL {q(FB_CH)};\n")
    step(f"복귀 4 {src} 덤프 → {back}")
    ex.sql(back, "SET GLOBAL super_read_only=OFF;\nSET GLOBAL read_only=OFF;\n"
                 f"SET SESSION sql_log_bin=0;\nDROP DATABASE IF EXISTS {cfg.db_name};\n")
    ex.load(back, data.encode())
    add = gtid(ex.sql(back, f"SELECT GTID_SUBTRACT({q(gset)}, @@GLOBAL.gtid_executed);\n"))
    if add:
        ex.sql(back, f"SET GLOBAL gtid_purged = {q('+' + add)};\n")
    step(f"복귀 5 {back} ← {src} 채널 {FB_CH}")
    ex.sql(back, source_sql(src, user, pw, cfg.db_name, FB_CH, 8640) + f"START REPLICA FOR CHANNEL {q(FB_CH)};\n")
    st = wait(ex, clock, back, lambda s: running(s) and s.get("lag") == 0, tries=100, channel=FB_CH)
    if not (running(st) and st.get("lag") == 0):
        raise RuntimeError(f"{back} 가 따라오지 못함: IO={st.get('io')} SQL={st.get('sql')} {st.get('error')}")
    return {"backup": bk, **st}


def switchover(ex, cfg, src, back, step, lock):
    step(f"전환 1 {src} 쓰기 멈춤 (super_read_only)")
    ex.sql(src, "SET PERSIST super_read_only=ON;\n")
    gt = gtid(ex.sql(src, "SELECT @@GLOBAL.gtid_executed;\n"))
    try:
        if ex.sql(back, f"SELECT WAIT_FOR_EXECUTED_GTID_SET({q(gt)}, 60);\n", 90).strip() != "0":
            raise RuntimeError(f"{back} 가 60초 안에 마지막 기록까지 받지 못함")
        step(f"전환 2 {back} 채널 해제 · 쓰기 가능 · 앱 계정 풀기")
        ex.sql(back, f"STOP REPLICA FOR CHANNEL {q(FB_CH)};\nRESET REPLICA ALL FOR CHANNEL {q(FB_CH)};\n"
                     "SET PERSIST super_read_only=OFF;\nSET PERSIST read_only=OFF;\n")
        lock(back, False)
    except Exception:
        step(f"   전환 실패 — {src} 다시 쓰기 가능")
        ex.sql(src, "SET PERSIST super_read_only=OFF;\nSET PERSIST read_only=OFF;\n")
        raise
    return {"gtid": gt}
