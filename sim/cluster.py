"""가짜 인프라 — pilotlight 가 실제로 보내는 SSH 명령 · SQL 문자열을 그대로 받아 해석한다.
GTID 집합 · binlog · relay · IO/SQL 스레드 · 계정 잠금 · 세션 · read_only · 네트워크 분할을 흉내 낸다."""
import json
import re
import time
import uuid as _uuid

from pilotlight.execs import SSHExec

CTL = "ctl"


def fmt(g):
    out = []
    for u in sorted(g):
        ns, parts, i = sorted(g[u]), [], 0
        while i < len(ns):
            j = i
            while j + 1 < len(ns) and ns[j + 1] == ns[j] + 1:
                j += 1
            parts.append(f"{ns[i]}" if i == j else f"{ns[i]}-{ns[j]}")
            i = j + 1
        if parts:
            out.append(f"{u}:" + ":".join(parts))
    return ",".join(out)


def parse(s):
    g = {}
    for item in filter(None, re.sub(r"\s", "", s or "").split(",")):
        u, *rs = item.split(":")
        for r in rs:
            a, _, b = r.partition("-")
            g.setdefault(u, set()).update(range(int(a), int(b or a) + 1))
    return g


def union(a, b):
    return {u: a.get(u, set()) | b.get(u, set()) for u in set(a) | set(b)}


def minus(a, b):
    return {u: v - b.get(u, set()) for u, v in a.items() if v - b.get(u, set())}


def subset(a, b):
    return not minus(a, b)


class Chan:
    def __init__(self):
        self.source, self.user, self.io, self.sql = None, None, False, False
        self.relay, self.connected, self.errno, self.error = [], False, 0, ""


class Server:
    def __init__(self, host, writer=False):
        self.host, self.uuid, self.up = host, str(_uuid.uuid4()), True
        self.ro = self.sro = not writer
        self.gtids, self.binlog, self.purged, self.rows = {}, [], {}, []
        self.users, self.sessions, self.chans, self.backups = {}, {}, {}, []
        self.db, self.sid, self._sid = True, 0, 100

    def has(self, u, n):
        return n in self.gtids.get(u, set())

    def commit(self, u, n, row, log=True):
        self.gtids.setdefault(u, set()).add(n)
        self.rows.append(row)
        if log:
            self.binlog.append((u, n, row))


class Cluster:
    def __init__(self):
        self.servers, self.apps, self.cut, self.t = {}, {}, set(), 0.0
        self.scaled, self.events = [], []

    def add(self, host, writer=False):
        self.servers[host] = Server(host, writer)
        return self.servers[host]

    def ok(self, a, b):
        sa, sb = self.servers.get(a), self.servers.get(b)
        if (sa and not sa.up) or (sb and not sb.up):
            return False
        return frozenset((a, b)) not in self.cut

    def partition(self, a, b, on=True):
        (self.cut.add if on else self.cut.discard)(frozenset((a, b)))

    def step(self):
        for s in self.servers.values():
            if not s.up:
                continue
            for c in s.chans.values():
                if c.io:
                    src = self.servers.get(c.source)
                    c.connected = bool(src and self.ok(s.host, src.host))
                    if not c.connected:
                        c.errno, c.error = 2003, f"Can't connect to MySQL server on '{c.source}:3306'"
                    elif (c.user, s.host) not in src.users:
                        c.connected, c.errno, c.error = False, 1045, f"Access denied for user '{c.user}'@'{s.host}'"
                    elif minus(src.purged, s.gtids):
                        c.connected, c.io = False, False
                        c.errno, c.error = 1236, "source has purged binary logs containing GTIDs that the replica requires"
                    else:
                        c.errno, c.error = 0, ""
                        have = {(u, n) for u, n, _ in c.relay}
                        c.relay += [e for e in src.binlog if not s.has(e[0], e[1]) and (e[0], e[1]) not in have]
                if c.sql:
                    for u, n, row in c.relay:
                        if not s.has(u, n):
                            s.commit(u, n, row)
                    c.relay = []

    def write(self, host, user, row):
        s = self.servers[host]
        if not s.up or not self.ok("client", host):
            return "down"
        if any(locked for (u, _), locked in s.users.items() if u == user) or not any(u == user for u, _ in s.users):
            return "locked"
        if s.ro or s.sro:
            return "read-only"
        s.commit(s.uuid, max(s.gtids.get(s.uuid, {0})) + 1, row)
        return "ok"

    def connect(self, host, user):
        s = self.servers[host]
        s._sid += 1
        s.sessions[s._sid] = user


class Clock:
    """가상 시계 — sleep 하면 시간이 가고 복제가 흐른다. pace > 0 이면 가상 1초마다 실제로 pace 초 쉰다 (화면으로 볼 때)."""

    def __init__(self, cluster, pace=0.0):
        self.c, self.pace = cluster, pace

    def now(self):
        return self.c.t

    def sleep(self, sec):
        for _ in range(max(1, int(sec))):
            self.c.t += min(sec, 1)
            self.c.step()
            if self.pace:
                time.sleep(self.pace)


class Probe:
    def __init__(self, cluster):
        self.c = cluster

    def tcp_ok(self, host, port, timeout=1.5):
        return host in self.c.servers and self.c.ok(CTL, host)

    def app_ready(self, host, port, path="/readyz", timeout=3):
        return bool(self.c.apps.get(host)) and self.c.ok(CTL, host)


class Power:
    def __init__(self, cluster, app_host):
        self.c, self.host = cluster, app_host

    def start(self, role):
        self.c.events.append(("start", role))
        if role == "app":
            self.c.apps[self.host] = True

    def stop(self, role):
        self.c.events.append(("stop", role))
        if role == "app":
            self.c.apps[self.host] = False

    def scale(self, n):
        self.c.scaled.append(n)


ACCT = r"'([^']*)'@'([^']*)'"


class Exec(SSHExec):
    """SSHExec 의 run 만 바꿔 끼운다 — sql · dump · load · script · status 는 실제 코드 그대로 지나온다."""

    def __init__(self, cluster, clock):
        super().__init__()
        self.c, self.clock = cluster, clock

    def run(self, host, remote, data=b"", timeout=600):
        s = self.c.servers.get(host)
        if not s or not self.c.ok(CTL, host):
            raise RuntimeError(f"ssh: connect to host {host} port 22: Connection timed out")
        if remote == "sudo -n true":
            return b""
        if "SHOW REPLICA STATUS" in remote:
            m = re.search(r"FOR CHANNEL '([^']*)'", remote)
            return self._status(s, m.group(1) if m else "").encode()
        if remote.startswith("sudo -n bash -s"):
            s.sid = int(remote.split()[-1])
            s.ro = s.sro = True
            return f"[mysql-replica] ready server_id={s.sid} gtid=ON super_read_only=1 version=8.4.6\n".encode()
        if "| gzip >" in remote:
            s.backups.append((re.search(r"gzip > (\S+)", remote).group(1), list(s.rows)))
            return b""
        if remote.startswith("sudo -n mysqldump"):
            return (f"-- fake mysqldump\nSET @@GLOBAL.GTID_PURGED=/*!80000 '+'*/ '{fmt(s.gtids)}';\n"
                    f"-- ROWS {json.dumps(s.rows)}\n").encode()
        if remote.startswith("sudo -n mysql --default-character-set"):
            return self._load(s, data.decode())
        if remote == "sudo -n mysql -N --batch":
            return "\n".join(x for x in (self.stmt(s, st.strip()) for st in data.decode().split(";")) if x).encode() + b"\n"
        raise RuntimeError(f"fake: 모르는 명령 {remote[:60]}")

    def _load(self, s, text):
        m = re.search(r"GTID_PURGED=(?:/\*!80000 '\+'\*/ )?'([^']*)'", text)
        if m:
            g = parse(m.group(1))
            s.gtids, s.purged = union(s.gtids, g), union(s.purged, g)
        rows = re.search(r"^-- ROWS (.*)$", text, re.M)
        s.rows, s.db = json.loads(rows.group(1)) if rows else [], True
        return b""

    def _status(self, s, ch):
        c = s.chans.get(ch)
        if not c:
            return ""
        io = "Yes" if c.io and c.connected else "Connecting" if c.io else "No"
        retrieved = union(s.gtids, {}) if c.io or c.relay else {}
        for u, n, _ in c.relay:
            retrieved.setdefault(u, set()).add(n)
        lag = len(c.relay) if io == "Yes" and c.sql else "NULL"
        return (f"*************************** 1. row ***************************\n"
                f"             Source_Host: {c.source}\n      Replica_IO_Running: {io}\n     Replica_SQL_Running: {'Yes' if c.sql else 'No'}\n"
                f"   Seconds_Behind_Source: {lag}\n           Last_IO_Errno: {c.errno}\n           Last_IO_Error: {c.error}\n"
                f"      Retrieved_Gtid_Set: {fmt(retrieved)}\n       Executed_Gtid_Set: {fmt(s.gtids)}\n           Auto_Position: 1\n")

    def stmt(self, s, x):
        if not x:
            return ""
        chm = re.search(r" FOR CHANNEL '([^']*)'$", x)
        ch = chm.group(1) if chm else ""
        x = x[:chm.start()] if chm else x
        if re.match(r"(CREATE USER|ALTER USER .* IDENTIFIED|GRANT|DROP DATABASE)", x) and s.sro:
            raise RuntimeError("ERROR 1290 (HY000): The MySQL server is running with the --super-read-only option")
        if x in ("SET SESSION sql_log_bin=0",) or x.startswith(("RESET PERSIST IF EXISTS", "CHANGE REPLICATION FILTER", "GRANT ")):
            return ""
        if m := re.fullmatch(r"SET (?:GLOBAL|PERSIST) (super_read_only|read_only)\s*=\s*(ON|OFF)", x):
            on = m.group(2) == "ON"
            if m.group(1) == "super_read_only":
                s.sro = on
                s.ro = s.ro or on
            else:
                s.ro = on
                s.sro = s.sro and on
            return ""
        if m := re.match(r"CREATE USER IF NOT EXISTS " + ACCT, x):
            s.users.setdefault((m.group(1), m.group(2)), False)
            return ""
        if re.match(r"ALTER USER " + ACCT + " IDENTIFIED", x):
            return ""
        if m := re.fullmatch(r"ALTER USER " + ACCT + r" ACCOUNT (LOCK|UNLOCK)", x):
            s.users[(m.group(1), m.group(2))] = m.group(3) == "LOCK"
            return ""
        if m := re.fullmatch(r"SELECT user, host FROM mysql\.user WHERE user IN \((.*)\)", x):
            want = re.findall(r"'([^']*)'", m.group(1))
            return "\n".join(f"{u}\t{h}" for u, h in s.users if u in want)
        if m := re.fullmatch(r"SELECT id FROM information_schema\.PROCESSLIST WHERE user IN \((.*)\)", x):
            want = re.findall(r"'([^']*)'", m.group(1))
            return "\n".join(str(i) for i, u in s.sessions.items() if u in want)
        if m := re.fullmatch(r"KILL (\d+)", x):
            s.sessions.pop(int(m.group(1)), None)
            return ""
        if x == "SELECT @@super_read_only":
            return str(int(s.sro))
        if x == "SELECT @@super_read_only, @@read_only, @@log_replica_updates":
            return f"{int(s.sro)}\t{int(s.ro)}\t1"
        if x == "SELECT CONCAT(@@read_only, @@super_read_only)":
            return f"{int(s.ro)}{int(s.sro)}"
        if x == "SELECT @@GLOBAL.gtid_executed":
            return fmt(s.gtids)
        if m := re.fullmatch(r"SELECT WAIT_FOR_EXECUTED_GTID_SET\('([^']*)', (\d+)\)", x):
            want = parse(m.group(1))
            for _ in range(int(m.group(2))):
                if subset(want, s.gtids):
                    return "0"
                self.clock.sleep(1)
            return "0" if subset(want, s.gtids) else "1"
        if m := re.fullmatch(r"SELECT GTID_SUBTRACT\('([^']*)', @@GLOBAL\.gtid_executed\)", x):
            return fmt(minus(parse(m.group(1)), s.gtids))
        if m := re.fullmatch(r"SELECT GTID_SUBSET\('([^']*)', @@GLOBAL\.gtid_executed\)", x):
            return "1" if subset(parse(m.group(1)), s.gtids) else "0"
        if m := re.fullmatch(r"SET GLOBAL gtid_purged = '\+([^']*)'", x):
            g = parse(m.group(1))
            s.gtids, s.purged = union(s.gtids, g), union(s.purged, g)
            return ""
        if m := re.fullmatch(r"SELECT COUNT\(\*\) FROM performance_schema\.replication_connection_configuration WHERE CHANNEL_NAME='([^']*)'", x):
            return "1" if m.group(1) in s.chans else "0"
        if re.fullmatch(r"SELECT COUNT\(\*\) FROM information_schema\.TABLES WHERE TABLE_SCHEMA='[^']*'", x):
            return "1" if s.db else "0"
        if re.fullmatch(r"DROP DATABASE IF EXISTS \w+", x):
            s.rows, s.db = [], False
            return ""
        if x == "RESET BINARY LOGS AND GTIDS":
            s.gtids, s.binlog, s.purged = {}, [], {}
            return ""
        if m := re.fullmatch(r"(STOP|START) REPLICA( IO_THREAD)?", x):
            c = s.chans.get(ch)
            if c:
                c.io = m.group(1) == "START"
                if not m.group(2):
                    c.sql = c.io
                c.connected = c.connected and c.io
            return ""
        if x == "RESET REPLICA ALL":
            s.chans.pop(ch, None) if chm else s.chans.clear()
            return ""
        if x.startswith("CHANGE REPLICATION SOURCE TO"):
            c = s.chans.setdefault(ch, Chan())
            if m := re.search(r"SOURCE_HOST='([^']*)'", x):
                c.source, c.relay = m.group(1), []
            if m := re.search(r"SOURCE_USER='([^']*)'", x):
                c.user = m.group(1)
            return ""
        raise RuntimeError(f"fake mysql: 지원하지 않는 문장 — {x[:80]}")
