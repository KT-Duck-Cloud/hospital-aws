"""AWS 관리 대시보드 API (데모) — sites/aws 화면이 부르는 주소 그대로. 통합 관제 서버(dashboard/server.py)가 넘긴다. 실제 계정 연동 없음.

  GET  /api/aws/status · /api/aws/services · /api/aws/tech?id= · /api/billing · /api/billing/budget · /api/billing/migration-estimate
       /api/connection · /api/otp/status · /api/pilot-light/status · /api/demo/instances · /api/demo/otp (데모 코드)
  POST /api/otp/verify · /api/otp/logout
  POST /api/billing/toggle · /api/billing/master · /api/aws/power/{on,off} · /api/pilot-light/action
       /api/demo/instance (VM 추가) · /api/demo/instance/remove            ← OTP 세션 필수 (없으면 403 otp_required)
"""
import json
import os
import time

from cloud import connection, services
from cloud.billing import Billing
from cloud.demo import TYPES, DemoAws
from cloud.otp import TTL, Otp, hotp
from cloud.site import Site

HERE = os.path.dirname(os.path.abspath(__file__))
PROTECTED = ("/api/billing/toggle", "/api/billing/master", "/api/aws/power/on", "/api/aws/power/off", "/api/pilot-light/action",
             "/api/demo/instance", "/api/demo/instance/remove")
MODES = {"pilot": "대기로 — vm1 끄기 · 워커 0", "promote": "2차 DR 발동 — 1조(t3 · t5) 장애 시연 → AWS 승격",
         "failback": "복귀 — t3 복구 → AWS 데이터로 맞춘 뒤 t3 로 절체"}


class Cloud:
    """dr() = DR 컨트롤 상태, board() = DR 현황 모양(phase · upstream), pilot(action) = 시연 단계 실행 → (성공, [기록])"""

    def __init__(self, aws=None, region="ap-southeast-2", asg="duckcloud-dr-app-asg", dr=lambda: {}, board=lambda: {},
                 pilot=None, otp=None, show_code=True):
        self.aws, self.region, self.board, self.pilot, self.show_code = aws or DemoAws(region, asg), region, board, pilot, show_code
        self.site = Site(self.aws, None, region, asg, dr)
        self.billing = Billing(self.aws, region, asg, primary=self.site.primary)
        self.site.b = self.billing
        self.otp = otp or Otp(os.environ.get("AWS_OTP_SECRET_FILE"))
        with open(os.path.join(HERE, "estimate.json"), encoding="utf-8") as f:
            self.estimate = json.load(f)

    def handles(self, path):
        return path.startswith(("/api/aws/", "/api/billing", "/api/otp/", "/api/pilot-light/", "/api/demo/")) or path == "/api/connection"

    def pl_status(self):
        b, dr = self.board() or {}, self.site.dr() or {}
        repl, vm1 = dr.get("repl") or {}, self.aws.on("vm1")
        active = self.site.primary()
        mode = "AWS_ACTIVE" if active else "WARM_STANDBY" if vm1 else "PILOT_LIGHT_STANDBY"
        src = {h: k for k, h in zip(("t3", "t5"), (dr.get("sites") or {}).get("site1_dbs", []))}.get(repl.get("source"), repl.get("source"))
        return {"mode": mode, "description": {"AWS_ACTIVE": "1조 장애로 AWS 두 대가 병원 서비스를 운영 중 — 끄기 거부",
                                              "WARM_STANDBY": "vm1 이 켜져 있지만 운영은 1조 — 과금 중 (대기로 되돌리기 가능)",
                                              "PILOT_LIGHT_STANDBY": "vm2 DB 만 켜서 1조를 실시간 복제 · vm1 정지 (컴퓨팅 $0)"}[mode],
                "ec2": {k: {"state": "running" if self.aws.on(k) else "stopped"} for k in ("vm1", "vm2")},
                "dbs": {"aws": {"read_only": not active, "lag": repl.get("lag", "-"), "source_host": src or "-"}},
                "current_upstream": b.get("upstream") or "unknown", "phase": b.get("phase"), "actions": MODES}

    def pl_action(self, action):
        if action not in MODES:
            return {"success": False, "error": "pilot | promote | failback 만"}
        if action == "pilot":
            if self.site.primary():
                return {"success": False, "error": "AWS 가 운영 중 — 먼저 복귀(failback)"}
            logs = []
            if self.aws.on("vm1"):
                self.aws(["ec2", "stop-instances", "--instance-ids", self.aws.vms["vm1"]["id"]])
                logs.append("vm1 정지 (컴퓨팅 과금 OFF)")
            if self.aws.workers():
                self.aws(["autoscaling", "set-desired-capacity", "--auto-scaling-group-name", self.site.asg, "--desired-capacity", "0"])
                logs.append("워커 ASG → 0")
            return {"success": True, "result": {"logs": logs or ["이미 Pilot Light 대기"]}}
        if not self.pilot:
            return {"success": False, "error": "이 서버에는 DR 시연이 연결되어 있지 않습니다"}
        try:
            ok, logs = self.pilot(action)
        except ValueError as e:
            return {"success": False, "error": str(e)}
        self.billing.cache["at"] = 0
        return {"success": ok, "result": {"logs": logs}, "error": "" if ok else (logs[-1] if logs else "실패")}

    def get(self, path, q, headers):
        route = {"/api/aws/status": lambda: self.site.summary(), "/api/aws/services": lambda: services.services(self.site, self.billing),
                 "/api/aws/tech": lambda: services.tech(q.get("id", "")), "/api/billing": lambda: self.billing.snapshot(q.get("force") == "1"),
                 "/api/billing/budget": lambda: self.billing.budget_status("000000000000"),
                 "/api/billing/migration-estimate": lambda: self.estimate, "/api/connection": lambda: connection.info(self.aws, self.region),
                 "/api/otp/status": lambda: self.otp.status(headers), "/api/pilot-light/status": self.pl_status,
                 "/api/demo/instances": lambda: {"instances": self.aws.extras(), "types": list(TYPES)},
                 "/api/demo/otp": lambda: {"code": hotp(self.otp.secret, int(time.time() // 30)) if self.show_code and self.otp.secret else None,
                                          "note": "데모 전용 — 실제 서버는 인증 앱에서"}}
        return (200, route[path](), ()) if path in route else (404, {"success": False, "error": "없는 주소"}, ())

    def post(self, path, body, headers):
        if path == "/api/otp/verify":
            ok, res = self.otp.verify(body.get("code"))
            if not ok:
                return 400, {"success": False, "error": res}, ()
            return 200, {"success": True, "token": res, "expires_in": TTL}, (
                ("Set-Cookie", f"duck_aws_otp={res}; Path=/; Max-Age={TTL}; HttpOnly; Secure; SameSite=Strict"),)
        if path == "/api/otp/logout":
            self.otp.logout(headers)
            return 200, {"success": True}, (("Set-Cookie", "duck_aws_otp=; Path=/; Max-Age=0; HttpOnly; Secure; SameSite=Strict"),)
        if path in PROTECTED and not self.otp.check(headers, body):
            return 403, {"success": False, "otp_required": True, "error": "과금 제어는 관리자 OTP(TOTP) 2단계 인증이 필요합니다."}, ()
        try:
            if path == "/api/billing/toggle":
                return 200, self.billing.toggle(body.get("key", ""), body.get("action", ""), body.get("value")), ()
            if path == "/api/billing/master":
                return 200, self.billing.master(body.get("mode", "")), ()
            if path in ("/api/aws/power/on", "/api/aws/power/off"):
                return 200, self.site.power(path.rsplit("/", 1)[1]), ()
            if path == "/api/pilot-light/action":
                return 200, self.pl_action(body.get("action")), ()
            if path == "/api/demo/instance":
                r = self.aws.add(body.get("name"), body.get("type", "t3.small"))
                self.billing.cache["at"] = 0
                return 200, {"success": True, "instance": r}, ()
            if path == "/api/demo/instance/remove":
                r = self.aws.remove(body.get("id"))
                self.billing.cache["at"] = 0
                return 200, {"success": True, **r}, ()
        except ValueError as e:
            return 400, {"success": False, "error": str(e)}, ()
        return 404, {"success": False, "error": "없는 주소"}, ()
