"""AWS t8 두 대 — 상태 · 전원 · 운영 형태. 온프레미스 t3 두 대를 1:1 로 대신한다.
  t3-vm1 .31 → t8-vm1 .81 (k3s 서버 + 앱, 평소 0개 · 정지)     t3-vm2 .32 → t8-vm2 .82 (MySQL 8.4 복제본 + 워커, 평소 켬)
운영 형태는 DR 컨트롤 상태에서 온다 — AWS 가 1조로 승격돼 있으면 끄기를 거부한다. 노드 접속은 SSH 가 아니라 SSM."""
import time

from cloud.billing import PRICING

NODES = [{"key": "vm1", "label": "vm1 앱", "name": "duckcloud-dr-control", "host": "t8-vm1", "vpn": "100.64.0.81", "disk_gb": 30,
          "role": "k3s 서버 · helm rsv (평소 0개)", "mirrors": {"host": "t3-vm1", "vpn": "100.64.0.31", "role": "control-plane · 앱"},
          "services": ["k3s server", "Traefik :30080", "web · auth · reservation · stats (승격 때만)"]},
         {"key": "vm2", "label": "vm2 DB", "name": "duckcloud-aws-vm2", "host": "t8-vm2", "vpn": "100.64.0.82", "disk_gb": 20,
          "role": "MySQL 8.4 복제본 · k3s 워커", "mirrors": {"host": "t3-vm2", "vpn": "100.64.0.32", "role": "worker · MySQL"},
          "services": ["MySQL 8.4 (GTID 복제 · read_only)", "k3s agent", "tailscale"]}]
VPC = {"cidr": "10.200.0.0/16", "subnet": "10.200.1.0/24 (공인 서브넷 · NAT 없음 · S3 Gateway Endpoint)"}
FLOW = [["평시", "vm2 가 1조 DB 를 GTID 로 복제 · vm1 정지 (컴퓨팅 과금 0)"],
        ["판정", "1조 DB 5분 무응답 + 복제 정상 + 증인 통과 → 승격"],
        ["승격", "vm2 쓰기 → vm1 켬 → 앱 /readyz 200 → 워커 ASG → 공개 입구 전환"],
        ["복귀", "1조 3분 응답 → AWS 데이터로 맞춤 → 절체 → vm1 정지 · AWS 다시 대기"]]


class Site:
    def __init__(self, aws, billing, region, asg, dr=lambda: {}):
        self.aws, self.b, self.region, self.asg, self.dr = aws, billing, region, asg, dr

    def ids(self):
        ok, d, _ = self.aws(["ec2", "describe-instances", "--filters", "Name=tag:duckcloud:role,Values=vm1,vm2"])
        inst = [i for rv in (d or {}).get("Reservations", []) for i in rv.get("Instances", [])] if ok else []
        return {next((t["Value"] for t in i.get("Tags", []) if t["Key"] == "duckcloud:role"), ""): i for i in inst}

    def nodes(self):
        by = self.ids()
        ok, d, _ = self.aws(["ssm", "describe-instance-information"])
        ssm = {x["InstanceId"]: x.get("PingStatus", "") for x in (d or {}).get("InstanceInformationList", [])} if ok else {}
        out = []
        for n in NODES:
            i = by.get(n["key"], {})
            st, it = (i.get("State") or {}).get("Name", "없음"), i.get("InstanceType", "t3.small")
            out.append({**n, "instance_id": i.get("InstanceId", ""), "state": st, "type": it, "private_ip": i.get("PrivateIpAddress", "-"),
                        "public_ip": i.get("PublicIpAddress", "-"), "launch_time": (i.get("LaunchTime") or "")[:19].replace("T", " "),
                        "ssm": ssm.get(i.get("InstanceId"), "Offline" if st == "running" else "-"),
                        "hourly": PRICING["ec2"].get(it, 0.0264) + (PRICING["public_ipv4_hour"] if i.get("PublicIpAddress") else 0)})
        return out

    def asg_status(self):
        ok, d, _ = self.aws(["autoscaling", "describe-auto-scaling-groups", "--auto-scaling-group-names", self.asg])
        g = ((d or {}).get("AutoScalingGroups") or [{}])[0] if ok else {}
        return {"name": self.asg, "min": g.get("MinSize", 0), "desired": g.get("DesiredCapacity", 0), "max": g.get("MaxSize", 2),
                "running": len(g.get("Instances", []))}

    def primary(self):
        return bool((self.dr() or {}).get("dr", {}).get("active"))

    def summary(self):
        nodes, dr = self.nodes(), self.dr() or {}
        run = sum(n["state"] == "running" for n in nodes)
        if self.primary():
            mode, desc = "운영(1조 승격)", "1조 장애로 AWS 두 대가 병원 서비스를 운영 중"
        elif run == 2 or (run == 1 and nodes[1]["state"] == "running"):
            mode, desc = "대기(Pilot Light)", "vm2 켜짐 · 1조 DB 복제 수신 · vm1 정지 · 앱 0개"
        elif run:
            mode, desc = "켜짐(일부)", "vm1 만 켜짐 — 복제 DB(vm2) 가 꺼져 승격 후보에서 빠짐"
        else:
            mode, desc = "꺼짐(과금 OFF)", "EBS 보관비만 · 승격 후보에서 빠짐"
        repl = dr.get("repl") or {}
        return {"timestamp": time.strftime("%Y-%m-%d %H:%M:%S"), "region": self.region, "vpc": VPC, "nodes": nodes, "running": run,
                "mode": mode, "mode_desc": desc, "flow": FLOW, "asg": self.asg_status(), "region_name": "Asia Pacific (Sydney)",
                "mesh": {"ok": bool(dr), "aws_is_primary": self.primary(), "aws_roles": ["AWS"] + (["1조"] if self.primary() else []),
                         "primary_member_label": "t8 (AWS)" if self.primary() else "1조 (온프레미스)", "at": time.strftime("%H:%M:%S"),
                         "repl": {"io": repl.get("io"), "sql": repl.get("sql"), "lag": repl.get("lag"), "source": repl.get("source")}}}

    def power(self, action):
        by = self.ids()
        ids = [i["InstanceId"] for i in by.values()]
        if not ids:
            return {"success": False, "error": "인스턴스를 찾지 못함 (태그 duckcloud:role · AWS 로그인 확인)"}
        if action == "off" and self.primary():
            return {"success": False, "error": "지금 2조 AWS 가 1조(운영)입니다 — 끄면 병원 서비스가 멈춥니다. 복귀 뒤 끄세요."}
        ok, _, err = self.aws(["ec2", "start-instances" if action == "on" else "stop-instances", "--instance-ids", *ids], timeout=30)
        self.b.log(f"aws-site-{action}", ",".join(ids), ok, err[-200:])
        self.b.cache["at"] = 0
        return {"success": ok, "action": action, "instances": ids, "error": err[-300:] if not ok else "",
                "next": "켜진 뒤 cloud/infra/up.sh --no-setup 로 설정 확인" if action == "on" else ""}
