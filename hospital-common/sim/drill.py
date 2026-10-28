"""모의 훈련 — 진짜 서비스 코드를 가짜 인프라(world.py) 위에서 돌리고, 단계마다 확인한다.

  [0] 평시          S1 · S2 입구 ON(메쉬) · Headscale S2 메인 → S1 로 5분 사본
  [1] 입구 감시      S1 입구 OFF + S2 전원 꺼짐 → 2번 연속 준비 안 됨 → S2 재시작 시도 → S1 입구 자동 ON
  [2] VPN 제어 승격  S2 Headscale 2분 무응답 → S1 이 최신 사본으로 승격 (가입 기록 유지) → 입구 ON
  [3] 두 주 서버 방지 S2 가 돌아와도 시작 전 가드가 거부 · 손으로 둘 다 켜면 보조(S1) 가 멈춤
  [4] 메인 복귀      S1 hs_stop (마지막 사본을 먼저 S2 로) → S2 복원 · 시작 (S1 승격 중 가입도 유지)
  [5] 콘솔          로그인 · CSRF · 5회 실패 잠금 · VM 사양 변경(끄기 → 변경 → 켜기 → 응답) · 보호 VM 거부 · 감사 기록
  [6] 공개 첫 화면    주소 숨김 · 로그인 뒤 주소 표시 · DR 전환을 따라 '지금 가는 곳' 이 바뀜
"""
import os

from services.entry.entry import watch
from services.hsha import ha
from sim import world as W


def run(say=print):
    w = W.World()
    S1, S2, n1, n2, e1 = w.gw["S1"], w.gw["S2"], w.node["S1"], w.node["S2"], w.entry["S1"]
    try:
        say("[0] 평시")
        W.join(n2, "t3-vm1", "100.64.0.31")
        W.join(n2, "t3-vm2", "100.64.0.32")
        ha.replicate(n2, n1.receive)
        assert e1.status()["mesh"] and w.entry["S2"].status()["connector_ready"]
        assert n2.running() and not n1.running() and n1.latest()[0]
        say(f"  S1 · S2 입구 ON · Headscale S2 메인 · S1 사본 {len(n1.latest()[0])} bytes")

        say("[1] S1 입구 OFF → S2 전원 꺼짐 → S1 감시")
        e1.stop({"by": "op"})
        st = {}
        assert watch(e1, st) == "peer-ok"
        W.join(n2, "t5-vm1", "100.64.0.51")
        ha.replicate(n2, n1.receive)
        S2.boot(False)
        seen = [watch(e1, st), watch(e1, st)]
        say(f"  감시 {seen}")
        assert seen == ["peer-fail 1", "self-started"] and S1.active("cloudflared-front") and e1.status()["mesh"]

        say("[2] S2 Headscale 무응답 → S1 승격")
        wt = w.watch("S1")
        ticks = [wt.tick() for _ in range(4)]
        say(f"  감시 {ticks}")
        assert ticks[-1] == "promoted" and n1.running()
        assert W.members(n1) == ["t3-vm1", "t3-vm2", "t5-vm1"], "사본의 가입 기록 유실"
        W.join(n1, "t8-vm2", "100.64.0.82")

        say("[3] S2 복귀 — 두 주 서버 방지")
        S2.boot(True)
        assert not n2.running(), "가드가 S2 시작을 막지 않음"
        try:
            w.entry["S2"].hs_start({"by": "op"})
            raise AssertionError("hs_start 가 거부되지 않음")
        except RuntimeError as e:
            say(f"  S2 hs_start 거부: {e}")
        S2.units["headscale"].update(active=True, enabled=True)          # 누군가 가드를 건너뛰고 켬
        assert w.guard["S1"].check() == "stopped-self" and not n1.running() and n2.running()
        say("  둘 다 실행 → 보조 S1 멈춤 (S2 메인)")
        S2.stop("headscale")
        S1.start("headscale")                                            # 다시 S1 이 주 서버 (가입 기록 그대로)

        say("[4] S2 메인 복귀")
        e1.hs_stop({"by": "op"})
        assert n2.latest()[0] and not n1.running()
        r = ha.promote(n2, W.health("S1"), log=lambda m: say("  " + m))
        assert r["running"] and W.members(n2) == ["t3-vm1", "t3-vm2", "t5-vm1", "t8-vm2"], "복귀 — S1 승격 중 가입 유실"
        assert os.path.isdir(r["backup"])
        say(f"  S2 복원 · 시작 — 가입 {len(W.members(n2))}대 · 이전 상태 백업 {os.path.basename(r['backup'])}")

        say("[5] 콘솔")
        con = w.console
        w.accounts.add("ops@example.org", "correct horse battery")
        for _ in range(5):
            assert w.accounts.verify("100.64.0.9", "ops@example.org", "wrong") in ("bad", "locked")
        assert w.accounts.verify("100.64.0.9", "ops@example.org", "correct horse battery") == "locked"
        w.accounts.unlock("ops@example.org")
        job = con.action("ops@example.org", "100.64.0.9", {"action": "resize", "name": "t3-vm2", "mem_mb": 8192})
        say("  " + " → ".join(m for _, m in job["steps"]))
        assert job["ok"] and w.vmm.vms["t3-vm2"]["mem_mb"] == 8192 and w.vmm.vms["t3-vm2"]["on"]
        job = con.action("ops@example.org", "100.64.0.9", {"action": "resize", "name": "t3-vm1", "mem_mb": 12288})
        assert not job["ok"] and "보호" in job["steps"][-1][1]
        job = con.action("ops@example.org", "100.64.0.9", {"action": "resize", "name": "t3-vm2", "mem_mb": 12288})
        assert not job["ok"] and "여유 메모리" in job["steps"][-1][1] and w.vmm.vms["t3-vm2"]["on"] is False
        con.action("ops@example.org", "100.64.0.9", {"action": "vm_start", "name": "t3-vm2"})
        st = con.state()
        assert st["errors"] == [] and st["entry"]["self"]["role"] == "S1" and st["entry"]["peer"]["role"] == "S2"
        say(f"  사양 변경 OK · 보호 VM 거부 · 메모리 부족 거부 · 입구 S1 {st['entry']['self']['connector_ready']} · S2 {st['entry']['peer']['connector_ready']}")

        say("[6] 공개 첫 화면")
        v = w.front.view()
        pub, det = w.front.cards(v, False), w.front.cards(v, True)
        assert "온프레미스 t3" in pub and "100.64.0.31" not in pub and "100.64.0.31" in det
        S1.write(os.path.join(w.tmp, "S1/etc/nginx/sites-available/hospital-frontdoor"), W.FRONTDOOR.format("100.64.0.81"))
        assert "AWS t8" in w.front.cards(w.front.view(), False)
        say("  공개: 주소 없음 · 로그인: 주소 표시 · DR 전환 → 'AWS t8 (2차 DR)'")
        say("\n기록")
        for m in w.log[-12:]:
            say(f"  {m}")
        say("\n모의 훈련 통과")
        return w
    finally:
        w.close()


if __name__ == "__main__":
    run()
