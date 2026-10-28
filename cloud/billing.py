"""과금 — 리전의 과금 대상 자원을 '기능' 목록으로 바꾸고, 기능 단위로 켜고 끈다.

  기능   ec2 (vm1 앱 · vm2 DB · 프로젝트 밖) · asg (추가 워커) · rds · alb · nat (설계상 없음 → 경고)
  고정비 공인 IPv4 · EBS · 스냅숏 — 꺼도 나가는 돈
  제어   최근 조회 목록의 key 만 (임의 자원 ID 주입 차단) · AWS 가 1조로 운영 중이면 끄기 거부
  전체   off = EC2 정지 + ASG 0 · demo = vm1 · vm2 함께 켬
"""
import threading
import time

PRICING = {"ec2": {"t3.micro": 0.0132, "t3.small": 0.0264, "t3.medium": 0.0528, "t3.large": 0.1056},
           "rds": {"db.t3.micro": 0.026, "db.t4g.micro": 0.024, "db.t4g.small": 0.048}, "rds_storage_gb_month": 0.138,
           "alb_hour": 0.0294, "nat_hour": 0.059, "public_ipv4_hour": 0.005, "ebs_gp3_gb_month": 0.096,
           "ebs_snapshot_gb_month": 0.055, "rds_snapshot_gb_month": 0.095}
HOURS = 730
ROLE = {"vm1": ("🖥️ vm1 앱 (k3s 서버 · 평소 0개)", "승격 때만 켬 — 평소 정지"),
        "vm2": ("🗄️ vm2 DB (MySQL 8.4 복제본 + 워커)", "평시에도 켬 — 1조 DB 실시간 복제")}


def tag(o, k):
    return next((t["Value"] for t in o.get("Tags", []) if t["Key"] == k), "")


class Billing:
    def __init__(self, aws, region, asg="", budget=100.0, primary=lambda: False, log=None):
        self.aws, self.region, self.asg, self.budget, self.primary = aws, region, asg, budget, primary
        self.cache, self.lock, self.actions = {"at": 0, "snap": None}, threading.Lock(), []
        self.log = log or (lambda *a: self.actions.append((time.strftime("%F %T"),) + a))

    def collect(self):
        q = {"ec2": ["ec2", "describe-instances"], "rds": ["rds", "describe-db-instances"], "alb": ["elbv2", "describe-load-balancers"],
             "nat": ["ec2", "describe-nat-gateways"], "eip": ["ec2", "describe-addresses"], "vol": ["ec2", "describe-volumes"],
             "snap": ["ec2", "describe-snapshots", "--owner-ids", "self"]}
        if self.asg:
            q["asg"] = ["autoscaling", "describe-auto-scaling-groups", "--auto-scaling-group-names", self.asg]
        res = {k: self.aws(v) for k, v in q.items()}
        d = {k: (r[1] if r[0] and isinstance(r[1], dict) else {}) for k, r in res.items()}
        feats, fixed, P, site = [], [], PRICING, f"2조 AWS(t8) · {self.region}"
        inst = [i for rv in d["ec2"].get("Reservations", []) for i in rv.get("Instances", [])]
        asg_on = []
        for i in inst:
            st, it = i["State"]["Name"], i.get("InstanceType", "")
            price, on = P["ec2"].get(it, 0.05), st in ("pending", "running")
            if tag(i, "aws:autoscaling:groupName"):
                asg_on += [price] if on else []
                continue
            role = tag(i, "duckcloud:role")
            label, note = ROLE.get(role, (f"🧩 추가 VM {tag(i, 'Name')}", "데모 화면에서 추가 — 쓰지 않으면 끄거나 삭제") if role == "extra" else
                                   (f"🖥️ EC2 {tag(i, 'Name') or i['InstanceId']} (프로젝트 밖)", "태그 없음 — 쓰지 않으면 정지"))
            feats.append({"key": f"ec2:{self.region}:{i['InstanceId']}", "site": site, "kind": "ec2", "name": label, "role": role,
                          "resource": f"{i['InstanceId']} · {tag(i, 'Name')} · {it}", "state": st, "on": on, "busy": st in ("pending", "stopping"),
                          "hourly_on": price, "hourly_now": price if on else 0.0, "fixed_month": 0.0, "controls": ["on", "off"],
                          "core": role in ROLE, "warn": role not in ROLE and role != "extra", "public_ip": i.get("PublicIpAddress", "-"),
                          "note": note + " · 정지해도 EBS 고정비"})
        for g in d.get("asg", {}).get("AutoScalingGroups", [])[:1]:
            feats.append({"key": f"asg:{self.region}:{g['AutoScalingGroupName']}", "site": site, "kind": "asg",
                          "name": "⚙️ 추가 워커 ASG (부족할 때만)", "resource": f"{g['AutoScalingGroupName']} · t3.small",
                          "state": f"desired {g['DesiredCapacity']} / max {g['MaxSize']} · 가동 {len(asg_on)}대", "on": g["DesiredCapacity"] > 0,
                          "busy": len(asg_on) != g["DesiredCapacity"], "desired": g["DesiredCapacity"], "max": g["MaxSize"],
                          "hourly_on": P["ec2"]["t3.small"], "hourly_now": round(sum(asg_on), 4), "fixed_month": 0.0, "controls": ["scale"],
                          "note": "평시 0 · 승격 뒤 DR 컨트롤이 늘린다"})
        for db in d["rds"].get("DBInstances", []):
            on, gb = db.get("DBInstanceStatus") not in ("stopped", "stopping", "deleting"), float(db.get("AllocatedStorage", 0))
            price = P["rds"].get(db.get("DBInstanceClass"), 0.05)
            feats.append({"key": f"rds:{self.region}:{db['DBInstanceIdentifier']}", "site": site, "kind": "rds",
                          "name": f"🗄️ RDS ({db['DBInstanceIdentifier']})", "resource": f"{db.get('DBInstanceClass')} · {int(gb)}GB",
                          "state": db.get("DBInstanceStatus"), "on": on, "busy": False, "hourly_on": price, "hourly_now": price if on else 0.0,
                          "fixed_month": round(gb * P["rds_storage_gb_month"], 2), "controls": ["on", "off"], "warn": True,
                          "note": "현재 구조에 없음 (DB 는 vm2 MySQL) — 남아 있으면 정지 후 삭제 검토"})
        for k, items, name, price in (("alb", d["alb"].get("LoadBalancers", []), "🔀 ALB", P["alb_hour"]),
                                      ("nat", d["nat"].get("NatGateways", []), "🚧 NAT Gateway", P["nat_hour"])):
            for x in items:
                rid = x.get("LoadBalancerName") or x.get("NatGatewayId")
                feats.append({"key": f"{k}:{self.region}:{rid}", "site": site, "kind": k, "name": f"{name} {rid}", "resource": rid,
                              "state": "과금 중", "on": True, "busy": False, "hourly_on": price, "hourly_now": price, "fixed_month": 0.0,
                              "controls": [], "warn": True, "note": "설계상 미사용 (공인 서브넷 + S3 Gateway Endpoint) → 삭제 검토"})
        eips = {a.get("PublicIp") for a in d["eip"].get("Addresses", [])}
        auto = [i for i in inst if i["State"]["Name"] == "running" and i.get("PublicIpAddress") and i["PublicIpAddress"] not in eips]
        vols, snaps = d["vol"].get("Volumes", []), d["snap"].get("Snapshots", [])
        for key, cond, name, res_, now, month, note in (
                ("ipv4", eips or auto, "🌐 공인 IPv4", f"EIP {len(eips)} · 자동 {len(auto)}", len(auto) * P["public_ipv4_hour"],
                 len(eips) * P["public_ipv4_hour"] * HOURS, "EIP 는 정지 중에도 시간당 과금"),
                ("ebs", vols, "💽 EBS 볼륨", f"{len(vols)}개 · {sum(v.get('Size', 0) for v in vols)}GB", 0.0,
                 sum(v.get("Size", 0) for v in vols) * P["ebs_gp3_gb_month"], "인스턴스 정지와 무관하게 GB-월"),
                ("snap", snaps, "📸 백업 스냅숏", f"{len(snaps)}개 · {sum(s.get('VolumeSize', 0) for s in snaps)}GB", 0.0,
                 sum(s.get("VolumeSize", 0) for s in snaps) * P["ebs_snapshot_gb_month"], "명목 GB 기준 상한 추정")):
            if cond:
                fixed.append({"key": f"fixed:{self.region}:{key}", "site": site, "kind": "fixed", "name": name, "resource": res_,
                              "state": "과금 중", "on": True, "busy": False, "hourly_on": 0.0, "hourly_now": round(now, 4),
                              "fixed_month": round(month, 2), "controls": [], "note": note})
        return feats + fixed, {k: r[2][:200] for k, r in res.items() if not r[0]}

    def snapshot(self, force=False, max_age=15):
        with self.lock:
            if not force and self.cache["snap"] and time.time() - self.cache["at"] < max_age:
                return self.cache["snap"]
        feats, errors = self.collect()
        hourly, fixed = sum(f["hourly_now"] for f in feats), sum(f["fixed_month"] for f in feats)
        snap = {"timestamp": time.strftime("%Y-%m-%d %H:%M:%S"), "region": self.region, "profile": self.aws.profile,
                "session": {**self.aws.session, "fix": "데모 리전 — 로그인 필요 없음"}, "features": feats, "errors": errors,
                "totals": {"hourly_now": round(hourly, 4), "daily_now": round(hourly * 24 + fixed / 30, 2), "fixed_month": round(fixed, 2),
                           "month_projection": round(hourly * HOURS + fixed, 2), "budget_limit": self.budget,
                           "running_count": sum(1 for f in feats if f["kind"] in ("ec2", "rds", "asg") and f["on"]),
                           "manual_delete": sum(1 for f in feats if f["kind"] in ("alb", "nat"))},
                "pricing_note": "추정 단가(온디맨드). 실제 청구액은 Budgets · Cost Explorer 기준"}
        with self.lock:
            self.cache.update(snap=snap, at=time.time())
        return snap

    def toggle(self, key, action, value=None):
        f = next((x for x in self.snapshot(max_age=60)["features"] if x["key"] == key), None)
        if not f:
            return {"success": False, "error": "현재 조회 목록에 없는 기능입니다. 새로고침 후 다시 시도하세요."}
        return self.apply(f, action, value)

    def apply(self, f, action, value=None):
        kind, _, rid = f["key"].split(":", 2)
        allowed = {"ec2": {"on", "off"}, "rds": {"on", "off"}, "asg": {"scale"}}
        if action not in allowed.get(kind, set()):
            return {"success": False, "error": f"{kind} 에는 {action} 을 할 수 없습니다"}
        if action == "off" and self.primary():
            return {"success": False, "error": "지금 2조 AWS 가 1조(운영)입니다 — 끄면 병원 서비스가 멈춥니다. 복귀 뒤 끄세요."}
        n = None
        if kind == "asg":
            n = max(0, min(int(value or 0), f.get("max", 2)))
            if n == 0 and self.primary():
                return {"success": False, "error": "AWS 운영 중 — 워커를 0 으로 줄이지 않습니다"}
        cmd = {("ec2", "on"): ["ec2", "start-instances", "--instance-ids", rid], ("ec2", "off"): ["ec2", "stop-instances", "--instance-ids", rid],
               ("rds", "on"): ["rds", "start-db-instance", "--db-instance-identifier", rid],
               ("rds", "off"): ["rds", "stop-db-instance", "--db-instance-identifier", rid],
               ("asg", "scale"): ["autoscaling", "set-desired-capacity", "--auto-scaling-group-name", rid, "--desired-capacity", str(n)]}[(kind, action)]
        ok, _, err = self.aws(cmd, timeout=30)
        self.log(f"{kind}-{action}", rid, ok, err[-200:])
        self.cache["at"] = 0
        return {"success": ok, "key": f["key"], "action": action, "value": n, "error": err[-300:] if not ok else ""}

    def master(self, mode):
        if mode not in ("off", "demo"):
            return {"success": False, "error": "mode 는 off | demo 만 허용됩니다."}
        if mode == "off" and self.primary():
            return {"success": False, "mode": mode, "actions": [], "error": "지금 2조 AWS 가 1조(운영)입니다 — 전체 절전을 거부합니다."}
        feats, out = self.snapshot(force=True)["features"], []
        for f in feats:
            if mode == "off" and f["kind"] in ("ec2", "rds") and f["on"] and not f["busy"]:
                out.append(self.apply(f, "off"))
            elif mode == "off" and f["kind"] == "asg" and f.get("desired"):
                out.append(self.apply(f, "scale", 0))
            elif mode == "demo" and f["kind"] == "ec2" and f.get("core") and not f["on"]:
                out.append(self.apply(f, "on"))
        return {"success": all(r["success"] for r in out), "mode": mode, "actions": out,
                "manual_delete_left": [f["name"] for f in feats if f["kind"] in ("alb", "nat")]}

    def budget_status(self, account):
        ok, d, err = self.aws(["budgets", "describe-budgets", "--account-id", account or "-"], region="us-east-1")
        if not ok:
            return {"success": False, "error": err[-200:]}
        return {"success": True, "budgets": [{"name": b["BudgetName"], "limit": float(b["BudgetLimit"]["Amount"]),
                                              "actual": float(((b.get("CalculatedSpend") or {}).get("ActualSpend") or {}).get("Amount", 0)),
                                              "forecast": float(((b.get("CalculatedSpend") or {}).get("ForecastedSpend") or {}).get("Amount", 0))}
                                             for b in (d or {}).get("Budgets", [])]}
