"""대기 사이트 월 비용 — 켜 둔 시간만 과금되는 EC2 · 항상 과금되는 EBS · 켜진 동안의 공인 IPv4.
단가는 예시다. 리전 · 시점마다 다르므로 AWS Pricing Calculator 값으로 바꿔 넣는다."""

HOURS = 730
PRICES = {"t3.small": 0.0264, "t3.medium": 0.0528, "ebs_gb_month": 0.096, "ipv4_hour": 0.005}
PROFILES = {
    "pilot-light": [("app", "t3.medium", 0, 20), ("db", "t3.small", HOURS, 20)],
    "warm-standby": [("app", "t3.medium", HOURS, 20), ("db", "t3.small", HOURS, 20)],
    "all-off": [("app", "t3.medium", 0, 20), ("db", "t3.small", 0, 20)],
}


def monthly(profile, prices=PRICES):
    rows = [{"role": r, "type": t, "hours": h, "ec2": h * prices[t], "ebs": gb * prices["ebs_gb_month"],
             "ipv4": h * prices["ipv4_hour"]} for r, t, h, gb in profile]
    return rows, round(sum(x["ec2"] + x["ebs"] + x["ipv4"] for x in rows), 2)


def failover_month(hours_on, prices=PRICES):
    """장애로 app 을 hours_on 시간 켰을 때의 그 달 비용."""
    return monthly([("app", "t3.medium", hours_on, 20), ("db", "t3.small", HOURS, 20)], prices)[1]


def table(prices=PRICES):
    out = []
    for name, prof in PROFILES.items():
        rows, total = monthly(prof, prices)
        out.append(f"{name:13} ${total:7.2f}/월  " + " · ".join(f"{x['role']} {x['type']} {x['hours']}h" for x in rows))
    out.append(f"{'failover 24h':13} ${failover_month(24, prices):7.2f}/월  pilot-light + app 24시간")
    return "\n".join(out)
