"""데모 AWS — 계정 · CLI 없이 메모리 안에서 도는 가짜 리전. cloud/ 의 화면 API 는 이것만 부른다 (실제 계정 연동 없음).

  인스턴스   vm1(앱) · vm2(DB) + 화면에서 추가한 VM — 켜기 · 끄기 · 추가 · 삭제(추가한 것만)
  워커 ASG   desired 0~2
  고정비     EBS(인스턴스 디스크 합) · 스냅숏 1개 · 공인 IPv4(켜진 인스턴스)
vm1 · vm2 의 켜짐은 link 로 바깥(시뮬레이터 DR)과 묶을 수 있다 — 승격이 vm1 을 켜면 화면도 켜짐.
"""
import itertools
import json
import threading
import time

from cloud.aws import Aws

TYPES = {"t3.micro": 8, "t3.small": 20, "t3.medium": 30, "t3.large": 50}     # 유형 → 기본 디스크 GB
MAX_EXTRA = 6


class DemoAws(Aws):
    def __init__(self, region="ap-southeast-2", asg="duckcloud-dr-app-asg", clock=time.time):
        super().__init__("demo", region)
        self.asg, self.now, self.lock, self.seq = asg, clock, threading.Lock(), itertools.count(3)
        self.vms = {"vm1": {"id": "i-0demo00000000001", "name": "duckcloud-dr-control", "type": "t3.small", "disk": 30, "on": False, "role": "vm1"},
                    "vm2": {"id": "i-0demo00000000002", "name": "duckcloud-aws-vm2", "type": "t3.small", "disk": 20, "on": True, "role": "vm2"}}
        self.desired = 0

    # 바깥과 묶을 때 덮어쓴다
    def on(self, key):
        return self.vms[key]["on"]

    def set(self, key, up):
        self.vms[key]["on"] = up

    def scale(self, n):
        self.desired = n

    def workers(self):
        return self.desired

    def add(self, name, itype="t3.small"):
        name = "".join(c for c in (name or "").lower() if c.isalnum() or c == "-")[:30].strip("-")
        if not name or itype not in TYPES:
            raise ValueError(f"이름(영문 · 숫자 · -)과 유형({', '.join(TYPES)})을 확인하세요")
        with self.lock:
            extra = [k for k, v in self.vms.items() if v["role"] == "extra"]
            if len(extra) >= MAX_EXTRA:
                raise ValueError(f"추가 VM 은 {MAX_EXTRA}대까지 (데모 예산)")
            if any(v["name"] == name for v in self.vms.values()):
                raise ValueError("같은 이름이 있습니다")
            n = next(self.seq)
            key = f"vm{n}"
            self.vms[key] = {"id": f"i-0demo{n:011d}", "name": name, "type": itype, "disk": TYPES[itype], "on": True, "role": "extra",
                             "launched": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(self.now()))}
            return {"key": key, **self.vms[key]}

    def remove(self, iid):
        with self.lock:
            key = next((k for k, v in self.vms.items() if v["id"] == iid), None)
            if not key or self.vms[key]["role"] != "extra":
                raise ValueError("화면에서 추가한 VM 만 지울 수 있습니다")
            return {"removed": self.vms.pop(key)["id"]}

    def extras(self):
        return [{"key": k, "id": v["id"], "name": v["name"], "type": v["type"], "on": self.on(k), "disk": v["disk"]}
                for k, v in self.vms.items() if v["role"] == "extra"]

    def inst(self, key):
        v, on = self.vms[key], self.on(key)
        n = int(v["id"][-2:])
        return {"InstanceId": v["id"], "InstanceType": v["type"], "State": {"Name": "running" if on else "stopped"},
                "PrivateIpAddress": f"10.200.1.{10 + n}", "LaunchTime": v.get("launched", "2026-10-28T09:00:00"),
                **({"PublicIpAddress": f"198.51.100.{10 + n}"} if on else {}),
                "Tags": [{"Key": "Name", "Value": v["name"]}, {"Key": "duckcloud:role", "Value": v["role"]}]}

    def run(self, args, region=None, timeout=15):
        a = " ".join(args[:2])
        by_id = {v["id"]: k for k, v in self.vms.items()}
        ids = [by_id[x] for x in args if x in by_id]
        want = next((args[i + 1].split("Values=", 1)[1].split(",") for i, x in enumerate(args) if x == "--filters" and "duckcloud:role" in args[i + 1]), None)
        keys = [k for k, v in self.vms.items() if want is None or v["role"] in want]
        w = self.workers()
        out = {"ec2 describe-instances": lambda: {"Reservations": [{"Instances": [self.inst(k) for k in keys]}]},
               "ec2 start-instances": lambda: [self.set(k, True) for k in ids] and {"StartingInstances": ids},
               "ec2 stop-instances": lambda: [self.set(k, False) for k in ids] and {"StoppingInstances": ids},
               "autoscaling describe-auto-scaling-groups": lambda: {"AutoScalingGroups": [
                   {"AutoScalingGroupName": self.asg, "MinSize": 0, "MaxSize": 2, "DesiredCapacity": w,
                    "Instances": [{"InstanceId": f"i-0worker{n}"} for n in range(w)]}]},
               "autoscaling set-desired-capacity": lambda: self.scale(int(args[args.index("--desired-capacity") + 1])) or {},
               "ec2 describe-volumes": lambda: {"Volumes": [{"Size": v["disk"]} for v in self.vms.values()]},
               "ec2 describe-snapshots": lambda: {"Snapshots": [{"VolumeSize": 20}]},
               "ec2 describe-addresses": lambda: {"Addresses": []}, "ec2 describe-nat-gateways": lambda: {"NatGateways": []},
               "rds describe-db-instances": lambda: {"DBInstances": []}, "elbv2 describe-load-balancers": lambda: {"LoadBalancers": []},
               "ssm describe-instance-information": lambda: {"InstanceInformationList": [
                   {"InstanceId": v["id"], "PingStatus": "Online"} for k, v in self.vms.items() if self.on(k)]},
               "sts get-caller-identity": lambda: {"Account": "000000000000", "Arn": "arn:aws:sts::000000000000:assumed-role/demo/viewer", "UserId": "DEMO"},
               "budgets describe-budgets": lambda: {"Budgets": [{"BudgetName": "demo-monthly", "BudgetLimit": {"Amount": "100"},
                                                                 "CalculatedSpend": {"ActualSpend": {"Amount": "6.42"}, "ForecastedSpend": {"Amount": "8.10"}}}]}}.get(a)
        if not out:
            return False, "", f"demo: {a} 는 데모에 없음"
        return True, json.dumps(out()), ""
