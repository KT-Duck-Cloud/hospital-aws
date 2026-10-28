"""/aws-services — 쓰는 AWS 서비스 목록(catalog.json: 무엇을 · 왜 · 어디 설정에서) + 지금 상태. 조회는 읽기 전용 API 만.
기술 상세의 코드 조각은 이 저장소 파일에서 그때그때 읽는다 (비밀값 모양은 가림)."""
import json
import os
import re
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SECRET = re.compile(r"(?i)((?:password|passwd|secret|token|api[_-]?key)\s*[=:]\s*[\"']?)([^\"'\s$]{8,})")


def catalog():
    try:
        with open(os.path.join(HERE, "catalog.json"), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError) as e:
        return {"services": [], "tech": [], "_error": str(e)}


def snippet(spec):
    rel = os.path.normpath(spec.get("file", ""))
    path = os.path.join(ROOT, rel)
    if rel.startswith("..") or not os.path.isfile(path):
        return {**spec, "ok": False, "error": "이 저장소에 없는 파일 (다른 담당 · 운영 서버 전용)"}
    with open(path, encoding="utf-8", errors="replace") as f:
        lines = f.read().splitlines()
    i = next((n for n, x in enumerate(lines) if spec.get("find") and spec["find"] in x), None)
    if i is None:
        return {**spec, "ok": False, "error": "찾는 줄이 없음 (코드가 바뀜)"}
    a, b = max(0, i - int(spec.get("before", 2))), min(len(lines), i + int(spec.get("after", 8)) + 1)
    return {**spec, "ok": True, "start": a + 1, "code": "\n".join(SECRET.sub(r"\1****", x) for x in lines[a:b])}


def tech(tid):
    cat = catalog()
    t = next((x for x in cat.get("tech", []) if x.get("id") == tid), None)
    if not t:
        return {"success": False, "error": f"'{tid}' 가 catalog.json 에 없음"}
    d = dict(t.get("detail") or {})
    d["snippets"] = [snippet(s) for s in d.get("snippets", [])]
    names = {x["id"]: x["service"] for x in cat.get("services", [])}
    return {"success": True, "tech": {k: v for k, v in t.items() if k != "detail"}, "detail": d,
            "services": [{"id": a, "name": names.get(a, a)} for a in t.get("aws", [])], "repo": {"base": "", "branch": "main"}}


def services(site, billing):
    snap, s = billing.snapshot(), site.summary()
    nodes, asg, feats = s["nodes"], s["asg"], snap["features"]
    fixed = {f["key"].split(":")[-1]: f for f in feats if f["kind"] == "fixed"}
    run = s["running"]
    live = {"ec2": {"state": " · ".join(f"{n['label']} {n['state']}" for n in nodes), "now": round(sum(n["hourly"] for n in nodes if n["state"] == "running"), 4),
                    "unit": "h", "on": run > 0},
            "ebs": {"state": (fixed.get("ebs") or {}).get("resource", "-"), "month": (fixed.get("ebs") or {}).get("fixed_month", 0), "on": True},
            "asg": {"state": f"desired {asg['desired']} / max {asg['max']} · 가동 {asg['running']}", "on": asg["desired"] > 0},
            "vpc": {"state": s["vpc"]["cidr"], "on": True, "free": True},
            "ssm": {"state": f"에이전트 {sum(n['ssm'] == 'Online' for n in nodes)}/2 Online", "on": True, "free": True},
            "cfn": {"state": "duckcloud-network · duckcloud-compute", "on": True, "free": True},
            "snap": {"state": (fixed.get("snap") or {}).get("resource", "-"), "month": (fixed.get("snap") or {}).get("fixed_month", 0), "on": True}}
    cat, tot = catalog(), snap["totals"]
    return {"success": True, "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"), "session": snap["session"], "region": s["region"],
            "summary": s, "live": live, "checks": {}, "features": cat.get("features", []), "checklist": cat.get("checklist", []),
            "presentation": cat.get("presentation", {}), "catalog": cat.get("services", []), "removed": cat.get("removed", []),
            "tech": cat.get("tech", []), "resource_map": cat.get("resource_map", {}),
            "catalog_meta": {"updated": cat.get("updated"), "file": "cloud/catalog.json", "error": cat.get("_error")},
            "inventory": [], "history": [],
            "totals": {"hourly_now": tot["hourly_now"], "fixed_month": tot["fixed_month"], "month_projection": tot["month_projection"],
                       "budget_limit": tot["budget_limit"], "on_hour_both": round(2 * (0.0264 + 0.005), 4)},
            "outside": [f["name"] + " — " + f["resource"] for f in feats if f["kind"] == "ec2" and not f.get("core")]}
