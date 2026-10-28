"""VPN ACL 편집 (관리 사이트 보안 화면, POST /acl/api) — hujson 원문을 주석째 보존하며 규칙만 고치고, 적용 전 차이 · 경로 시험.

  op load · model · edit(add | update | delete | move) · preview(diff) · apply(base_sha 가 같을 때만) · version · test(src → dst:port)
  저장: Policy(path) — 적용할 때마다 versions/ 에 사본 + sha256, reload() 로 Headscale 에 반영 (실제: headscale policy set -f)
"""
import difflib
import hashlib
import ipaddress
import json
import os
import time


def tokens(t):
    out, i, n = [], 0, len(t)
    while i < n:
        c = t[i]
        if c.isspace():
            i += 1
        elif c == '"':
            j = i + 1
            while j < n and t[j] != '"':
                j += 2 if t[j] == "\\" else 1
            out.append(("str", i, j + 1))
            i = j + 1
        elif t.startswith("//", i) or t.startswith("/*", i):
            line = t[i + 1] == "/"
            j = t.find("\n", i) if line else t.find("*/", i + 2)
            j = n if j < 0 else j if line else j + 2
            out.append(("cmt", i, j))
            i = j
        elif c in "{}[],:":
            out.append((c, i, i + 1))
            i += 1
        else:
            j = i
            while j < n and not t[j].isspace() and t[j] not in '{}[],:"/':
                j += 1
            out.append(("lit", i, max(j, i + 1)))
            i = max(j, i + 1)
    return out


def to_json(t):
    tk = tokens(t)
    keep = [x for x in tk if x[0] != "cmt"]
    return " ".join(t[s:e] for k, (kind, s, e) in enumerate(keep) if not (kind == "," and (k + 1 == len(keep) or keep[k + 1][0] in "]}")))


def split_acls(t):
    """→ (앞, [(주석들, 규칙 원문)], 끝 주석들, 뒤)"""
    tk, depth = tokens(t), 0
    for k, x in enumerate(tk):
        depth += x[0] in "{[" or -(x[0] in "}]")
        if x[0] == "str" and depth == 1 and t[x[1]:x[2]] == '"acls"':
            m = [j for j in range(k + 1, len(tk)) if tk[j][0] != "cmt"]
            if len(m) > 1 and tk[m[0]][0] == ":" and tk[m[1]][0] == "[":
                return _walk(t, tk, m[1])
    raise ValueError('"acls" 목록을 찾지 못함')


def _walk(t, tk, o):
    elems, cm, k = [], [], o + 1
    while k < len(tk):
        x = tk[k]
        if x[0] == "cmt":
            cm.append(t[x[1]:x[2]].strip())
        elif x[0] == "]":
            return t[:tk[o][2]], elems, cm, t[x[1]:]
        elif x[0] != ",":
            d, s = 0, x[1]
            while k < len(tk):
                d += tk[k][0] in "{[" or -(tk[k][0] in "}]")
                if d == 0 and tk[k][0] != "cmt":
                    break
                k += 1
            elems.append((cm, t[s:tk[k][2]]))
            cm = []
        k += 1
    raise ValueError("acls 목록이 닫히지 않음")


def join_acls(head, elems, tail, rest, ind="    "):
    body = ",\n".join("".join(f"{ind}{c}\n" for c in cm) + ind + b for cm, b in elems)
    return head + "\n" + body + "".join(f"\n{ind}{c}" for c in tail) + "\n  " + rest


def model(t):
    try:
        p, (_, elems, _, _) = json.loads(to_json(t)), split_acls(t)
    except (ValueError, json.JSONDecodeError) as e:
        return {"error": f"형식 오류 — 원문 탭에서 고치세요: {e}", "rules": [], "hosts": {}, "groups": {}}
    rules = []
    for i, (cm, b) in enumerate(elems):
        try:
            r = json.loads(to_json(b))
        except json.JSONDecodeError:
            r = {}
        rules.append({"i": i, "comment": "\n".join(c.lstrip("/* ").rstrip("*/ ") for c in cm), "action": r.get("action", ""),
                      "src": r.get("src", []), "dst": r.get("dst", []), "extra": {k: v for k, v in r.items() if k not in ("action", "src", "dst")}})
    return {"rules": rules, "hosts": p.get("hosts", {}), "groups": p.get("groups", {}), "tagOwners": p.get("tagOwners", {}), "error": ""}


def clean(v, what):
    out = [x.strip().strip('"') for x in (v.replace(",", "\n").split("\n") if isinstance(v, str) else v or []) if str(x).strip().strip('"')]
    if not out:
        raise ValueError(f"{what}이 비어 있음")
    return out


def edit(t, kind, i=None, rule=None, direction=0):
    head, elems, tail, rest = split_acls(t)
    n = len(elems)
    if kind in ("update", "delete", "move") and not (isinstance(i, int) and 0 <= i < n):
        raise ValueError("없는 규칙 — 새로 읽으세요")
    if kind in ("add", "update"):
        rule = rule or {}
        src, dst = clean(rule.get("src"), "출발"), clean(rule.get("dst"), "도착")
        bad = [d for d in dst if ":" not in d]
        if bad:
            raise ValueError(f"도착에 포트가 없음: {', '.join(bad)} (예: t3@:22 · 100.64.0.31:22,80)")
        body = "{ " + ", ".join(['"action": "accept"', f'"src": {json.dumps(src)}', f'"dst": {json.dumps(dst)}']) + " }"
        cm = [f"// {x.strip()}" for x in str(rule.get("comment") or "").splitlines() if x.strip()]
        if kind == "add":
            elems.insert(n if i is None or not 0 <= i < n else i + 1, (cm, body))
        else:
            elems[i] = (cm, body)
    elif kind == "delete":
        elems.pop(i)
    elif kind == "move":
        j = i + (1 if direction > 0 else -1)
        if 0 <= j < n:
            elems[i], elems[j] = elems[j], elems[i]
    else:
        raise ValueError("알 수 없는 편집")
    return join_acls(head, elems, tail, rest)


def _ports(spec):
    if spec == "*":
        return lambda p: True
    rng = [tuple(int(x) for x in (part.split("-") + [part])[:2]) for part in spec.split(",") if part.strip().split("-")[0].isdigit()]
    return lambda p: any(a <= p <= b for a, b in rng)


def _match(alias, node, pol):
    if alias == "*":
        return True
    if alias.endswith("@"):
        return node.get("user") == alias[:-1]
    if alias.startswith("group:"):
        return any(_match(m, node, pol) for m in pol.get("groups", {}).get(alias, []))
    if alias.startswith("tag:"):
        return alias in node.get("tags", [])
    try:
        return ipaddress.ip_address(node.get("ip", "")) in ipaddress.ip_network(pol.get("hosts", {}).get(alias, alias), strict=False)
    except ValueError:
        return False


def test(t, nodes, src, dst, port):
    pol, m = json.loads(to_json(t)), model(t)
    find = lambda q: next((n for n in nodes if q in (n["ip"], n["name"])), None)  # noqa: E731
    a, b = find((src or "").strip()), find((dst or "").strip())
    if not a or not b:
        raise ValueError(f"노드를 찾지 못함: {src if not a else dst} (이름 또는 VPN 주소)")
    port, hit, anyp = int(port), set(), set()
    for r in m["rules"]:
        if any(_match(s, a, pol) for s in r["src"]):
            for d in r["dst"]:
                al, _, ps = d.rpartition(":")
                if _match(al, b, pol):
                    anyp.add(r["i"])
                    if _ports(ps)(port):
                        hit.add(r["i"])
    return {"src": a, "dst": b, "port": port, "allowed": bool(hit), "rules": sorted(hit), "ping": bool(anyp), "ping_rules": sorted(anyp),
            "suggest": {"comment": f"{a['name']} → {b['name']} :{port} 허용", "src": [a["ip"]], "dst": [f"{b['ip']}:{port}"]}}


def _load(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def _json(path, data=None):
    with open(path, "w" if data is not None else "r", encoding="utf-8") as f:
        return json.dump(data, f, ensure_ascii=False) if data is not None else json.load(f)


class Policy:
    def __init__(self, path, reload=lambda text: None, clock=time.time):
        self.path, self.reload, self.now = path, reload, clock
        self.vdir = path + ".versions"

    def get(self):
        t = _load(self.path)
        return {"content": t, "sha": hashlib.sha256(t.encode()).hexdigest()}

    def history(self):
        return sorted((_json(os.path.join(self.vdir, f)) | {"content": None} for f in os.listdir(self.vdir) if f.endswith(".json")),
                      key=lambda v: -v["version"]) if os.path.isdir(self.vdir) else []

    def apply(self, content, base_sha, by, note=""):
        cur = self.get()
        if base_sha != cur["sha"]:
            raise ValueError("그 사이 다른 사람이 바꿨습니다 — 새로 읽은 뒤 다시")
        json.loads(to_json(content))
        split_acls(content)
        if content == cur["content"]:
            return {"changed": False, "sha": cur["sha"]}
        self.reload(content)
        os.makedirs(self.vdir, exist_ok=True)
        v = len(self.history()) + 1
        sha = hashlib.sha256(content.encode()).hexdigest()
        _json(os.path.join(self.vdir, f"{v:04d}.json"), {"version": v, "sha": sha, "by": by, "note": note, "at": self.now(), "content": cur["content"]})
        with open(self.path + ".tmp", "w", encoding="utf-8") as f:
            f.write(content)
        os.replace(self.path + ".tmp", self.path)
        return {"changed": True, "version": v, "sha": sha}

    def version(self, i):
        return _json(os.path.join(self.vdir, f"{int(i):04d}.json"))


def api(pol, nodes, req, email):
    """→ (상태, dict)"""
    op, c = req.get("op"), str(req.get("content") or "")
    try:
        if op == "load":
            g = pol.get()
            return 200, {"ok": True, "live": g, "model": model(g["content"]), "nodes": nodes(), "versions": pol.history()}
        if op == "model":
            return 200, {"ok": True, "model": model(c)}
        if op == "edit":
            new = edit(c, req.get("kind"), None if req.get("i") is None else int(req["i"]), req.get("rule"), int(req.get("dir") or 0))
            return 200, {"ok": True, "content": new, "model": model(new)}
        if op == "preview":
            g = pol.get()
            return 200, {"ok": True, "diff": list(difflib.unified_diff(g["content"].splitlines(), c.splitlines(), "적용 중", "초안", n=2, lineterm="")),
                         "check": {"ok": not model(c)["error"], "error": model(c)["error"]}, "live_sha": g["sha"]}
        if op == "apply":
            return 200, {"ok": True, **pol.apply(c, req.get("base_sha"), email, str(req.get("note") or "")[:300])}
        if op == "version":
            return 200, {"ok": True, **pol.version(req.get("id"))}
        if op == "test":
            return 200, {"ok": True, **test(c or pol.get()["content"], nodes(), req.get("src"), req.get("dst"), req.get("port"))}
        return 400, {"ok": False, "error": "알 수 없는 요청"}
    except (ValueError, TypeError, KeyError, OSError, json.JSONDecodeError) as e:
        return 400, {"ok": False, "error": str(e)}
