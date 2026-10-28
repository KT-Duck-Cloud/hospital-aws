"""AWS 연결 구조 — 이 대시보드가 어떤 API 를 언제 · 어떤 권한으로 부르는지(실제 호출 수 포함), 최소 권한 IAM 정책, 콘솔 바로가기."""
import json

# (API, 용도, 호출 시점, IAM 권한, 쓰기)
CATALOG = [
    ("sts get-caller-identity", "계정 · 로그인 주체 확인", "연결 화면", "sts:GetCallerIdentity", False),
    ("ec2 describe-instances", "vm1 · vm2 상태 · 공인 IP", "상태 5초 · 과금 15초", "ec2:DescribeInstances", False),
    ("ec2 describe-addresses", "EIP 감지 — 설계상 0개", "과금 15초", "ec2:DescribeAddresses", False),
    ("ec2 describe-volumes", "EBS 고정비", "과금 15초", "ec2:DescribeVolumes", False),
    ("ec2 describe-snapshots", "백업 스냅숏 고정비", "과금 15초", "ec2:DescribeSnapshots", False),
    ("ec2 describe-nat-gateways", "NAT 감지 (설계상 미사용)", "과금 15초", "ec2:DescribeNatGateways", False),
    ("autoscaling describe-auto-scaling-groups", "추가 워커 desired · 가동 수", "상태 · 과금", "autoscaling:DescribeAutoScalingGroups", False),
    ("rds describe-db-instances", "RDS 잔존 감지", "과금 15초", "rds:DescribeDBInstances", False),
    ("elbv2 describe-load-balancers", "ALB 감지 (정지 불가 → 삭제 대상)", "과금 15초", "elasticloadbalancing:DescribeLoadBalancers", False),
    ("ssm describe-instance-information", "SSM 에이전트 Online", "상태 5초", "ssm:DescribeInstanceInformation", False),
    ("budgets describe-budgets", "이번 달 실제 · 예측 청구액", "10분", "budgets:ViewBudget", False),
    ("ec2 start-instances", "vm1 · vm2 켜기", "토글 · 시연 모드", "ec2:StartInstances", True),
    ("ec2 stop-instances", "vm1 · vm2 끄기 (AWS 가 1조면 거부)", "토글 · 전체 절전", "ec2:StopInstances", True),
    ("rds stop-db-instance", "RDS 끄기 (잔존 시)", "토글 · 전체 절전", "rds:StopDBInstance", True),
    ("autoscaling set-desired-capacity", "워커 ASG 0~2", "토글 · 전체 절전 · DR 승격", "autoscaling:SetDesiredCapacity", True),
    ("ssm send-command", "vm2 MySQL 가동 · 정지 (SSH 없음)", "버튼", "ssm:SendCommand", True),
]


def policy():
    acts = lambda w: sorted({c[3] for c in CATALOG if c[4] == w})  # noqa: E731
    return json.dumps({"Version": "2012-10-17", "Statement": [
        {"Sid": "DashboardReadOnly", "Effect": "Allow", "Action": acts(False), "Resource": "*"},
        {"Sid": "DashboardCostControl", "Effect": "Allow", "Action": acts(True), "Resource": "*",
         "Condition": {"StringEquals": {"aws:ResourceTag/duckcloud:role": ["vm1", "vm2", "worker"]}}}]}, indent=2)


def info(aws, region):
    ok, d, err = aws(["sts", "get-caller-identity"])
    ident = {"ok": ok, "account": (d or {}).get("Account"), "arn": (d or {}).get("Arn"), "error": "" if ok else err.splitlines()[-1:][0][:200] if err else ""}
    stats = aws.stats
    c = f"https://{region}.console.aws.amazon.com"
    return {"success": True, "identity": ident, "session": aws.session, "region": region, "profile": aws.profile,
            "cli": {"auth_kind": "데모 — 실제 AWS 계정에 연결하지 않음 (cloud/demo.py 가 메모리 안에서 응답)", "profile_rows": [], "version": "demo"},
            "apis": [{"api": a, "usage": u, "timing": t, "permission": p, "is_write": w, "endpoint": "region",
                      **{k: v for k, v in (stats.get(a) or {"count": 0, "fail": 0}).items() if k != "api"}} for a, u, t, p, w in CATALOG],
            "iam_policy": policy(),
            "consoles": [{"name": "EC2 Instances", "desc": "vm1 · vm2", "url": f"{c}/ec2/home?region={region}#Instances:", "icon": "fa-server"},
                         {"name": "Auto Scaling Groups", "desc": "추가 워커 0~2", "url": f"{c}/ec2/home?region={region}#AutoScalingGroups:", "icon": "fa-layer-group"},
                         {"name": "CloudFormation", "desc": "network · compute", "url": f"{c}/cloudformation/home?region={region}#/stacks", "icon": "fa-cubes"},
                         {"name": "Systems Manager", "desc": "Run Command · Parameter Store", "url": f"{c}/systems-manager/run-command?region={region}", "icon": "fa-terminal"},
                         {"name": "Cost Explorer", "desc": "비용 추이", "url": "https://us-east-1.console.aws.amazon.com/costmanagement/home#/cost-explorer", "icon": "fa-chart-pie"}]}
