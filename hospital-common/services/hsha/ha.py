"""Headscale 주 · 대기 — 두 서버가 함께 주 서버가 되지 않게 (DB 갈라짐 방지). S2 가 메인, S1 이 보조.

  replicate   주 서버 → 대기 서버로 5분마다 사본 (멈추기 직전에도 한 번)
  Guard.pre   시작 전: 다른 서버 Headscale 이 돌고 있으면 시작 거부
  Guard.check 보조(S1)만 1분마다: 둘 다 돌고 있으면 보조를 멈춘다 — 메인이 이긴다
  Watch       대기 서버 30초마다: 주 서버가 두 길(WireGuard · 공인) 모두 응답 없음 4번 연속(약 2분) → 최신 사본으로 승격 → 입구 켬
  promote     최신 사본 복원 → Headscale 켬 (주 서버가 살아 있으면 거부, --force 는 감시만)
다른 서버 확인: 그 게이트 :63728/health 를 X-Duck-Gate 머리글로 — 게이트가 넘기지 않고 자기 Headscale 만 답한다.
"""
MAIN = "S2"


def peer_running(host, addrs, who="guard"):
    for url in addrs:
        code, body = host.http(url, {"X-Duck-Gate": who}, timeout=5, insecure=True)
        if code == 200 and b"pass" in body:
            return True
    return False


def replicate(src, send):
    """src = 주 서버 HsNode, send(blob, at) = 대기 서버로 (실제는 ssh · 시뮬레이터는 함수)"""
    if not src.running():
        return {"skipped": "주 서버 아님"}
    blob = src.snapshot()
    send(blob, src.h.now())
    return {"bytes": len(blob)}


class Guard:
    def __init__(self, node, peer_addrs, log=lambda m: None):
        self.n, self.addrs, self.log = node, peer_addrs, log

    def pre(self):
        if peer_running(self.n.h, self.addrs):
            self.log(f"{self.n.role} guard 다른 서버가 주 서버 — 시작 거부")
            raise RuntimeError("다른 서버 Headscale 이 주 서버입니다 — 전환은 콘솔에서")

    def check(self):
        if self.n.role == MAIN or not self.n.running():
            return "ok"
        if peer_running(self.n.h, self.addrs):
            self.n.h.stop("headscale")
            self.log(f"{self.n.role} guard 둘 다 실행 — {self.n.role} 멈춤 ({MAIN} 메인)")
            return "stopped-self"
        return "ok"


def promote(node, peer_addrs, force=False, log=lambda m: None):
    blob, at = node.latest()
    if not blob:
        raise RuntimeError("사본 없음")
    if not force and peer_running(node.h, peer_addrs):
        raise RuntimeError("주 서버가 살아 있습니다 — 두 서버가 함께 돌면 DB 가 갈라집니다")
    node.h.stop("headscale")
    r = node.restore(blob)
    node.h.start("headscale")
    log(f"{node.role} 승격 — 사본 시각 {at:.0f}")
    return {**r, "snapshot_at": at, "running": node.running()}


def demote(node):
    node.h.stop("headscale")
    return {"running": node.running()}


class Watch:
    """state = {"n": 연속 실패 수} — 타이머로 따로 도는 프로세스 사이에 남는다 (실제는 /run 파일)"""

    def __init__(self, node, peer_addrs, entry_start, limit=4, log=lambda m: None, state=None):
        self.n, self.addrs, self.entry_start, self.limit, self.log = node, peer_addrs, entry_start, limit, log
        self.st = {"n": 0} if state is None else state

    def tick(self):
        if self.n.running() or peer_running(self.n.h, self.addrs, "watch"):
            self.st["n"] = 0
            return "self-primary" if self.n.running() else "peer-ok"
        self.st["n"] = self.st.get("n", 0) + 1
        self.log(f"{self.n.role} 주 서버 Headscale 응답 없음 ({self.st['n']}/{self.limit})")
        if self.st["n"] < self.limit:
            return f"miss {self.st['n']}"
        self.st["n"] = 0
        try:
            promote(self.n, self.addrs, force=True, log=self.log)
        except Exception as e:
            self.log(f"{self.n.role} 승격 실패 — {e}")
            return "promote-failed"
        self.entry_start()
        self.log(f"{self.n.role} 승격 완료 · 입구 켬")
        return "promoted"
