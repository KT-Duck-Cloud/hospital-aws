"""Pilot Light 전원 — 평시 app(vm1) 정지 · db(vm2) 기동. 승격 때 app 기동 + 워커 ASG 확장, 복귀 때 원위치."""
import json
import subprocess


class Ec2Power:
    def __init__(self, cfg, aws="aws"):
        self.cfg, self.aws, self._ids = cfg, aws, {}

    def _call(self, *args, timeout=900):
        argv = [self.aws, *args, "--output", "json"] + (["--region", self.cfg.aws_region] if self.cfg.aws_region else [])
        r = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
        if r.returncode:
            raise RuntimeError(r.stderr.strip()[-300:] or f"aws exit {r.returncode}")
        return json.loads(r.stdout) if r.stdout.strip() else {}

    def instance(self, role):
        name = {"app": self.cfg.aws_app_name, "db": self.cfg.aws_db_name}[role]
        if name not in self._ids:
            out = self._call("ec2", "describe-instances", "--filters", f"Name=tag:Name,Values={name}",
                             "Name=instance-state-name,Values=pending,running,stopping,stopped",
                             "--query", "Reservations[].Instances[].InstanceId")
            if len(out) != 1:
                raise RuntimeError(f"태그 Name={name} 인스턴스가 {len(out)}개")
            self._ids[name] = out[0]
        return self._ids[name]

    def state(self, role):
        return self._call("ec2", "describe-instances", "--instance-ids", self.instance(role),
                          "--query", "Reservations[0].Instances[0].State.Name")

    def start(self, role):
        i = self.instance(role)
        self._call("ec2", "start-instances", "--instance-ids", i)
        self._call("ec2", "wait", "instance-running", "--instance-ids", i)

    def stop(self, role):
        i = self.instance(role)
        self._call("ec2", "stop-instances", "--instance-ids", i)
        self._call("ec2", "wait", "instance-stopped", "--instance-ids", i)

    def scale(self, n):
        if self.cfg.aws_asg:
            self._call("autoscaling", "set-desired-capacity", "--auto-scaling-group-name", self.cfg.aws_asg,
                       "--desired-capacity", str(n))


def app_up(ctx, step):
    c = ctx.cfg
    step(f"앱 1 {c.aws_app_name} 기동 (EC2 start · wait running)")
    ctx.power.start("app")
    for i in range(c.ready_tries):
        if ctx.probe.app_ready(c.standby_app, c.app_port, c.ready_path):
            step(f"앱 2 {c.standby_app}:{c.app_port}{c.ready_path} 200 ({i * 3}초)")
            break
        ctx.clock.sleep(3)
    else:
        raise RuntimeError(f"{c.standby_app}{c.ready_path} 가 {c.ready_tries * 3}초 안에 200 이 아님")
    if c.aws_asg:
        step(f"앱 3 워커 ASG {c.aws_asg} → {c.aws_workers}")
        ctx.power.scale(c.aws_workers)
    return {"app": "up"}


def app_down(ctx, step):
    step("앱 정지 — 워커 0 · app 인스턴스 정지 (db 는 복제 수신으로 켜 둔다)")
    ctx.power.scale(0)
    ctx.power.stop("app")
    return {"app": "down"}
