"""Headscale 어댑터 — CLI(JSON) 로 조회 · 키 발급 · 삭제, 주소 고정만 SQLite 를 직접 고친다 (CLI 에 기능이 없음)."""
import json
import os
import shutil
import sqlite3
import subprocess
import time


def _ip4(n):
    return next((a for a in n.get("ip_addresses") or n.get("ipAddresses") or [] if "." in a), "")


class Headscale:
    def __init__(self, db="/var/lib/headscale/db.sqlite", bin="headscale", unit="headscale",
                 backup_dir="/var/backups/pilotlight-vpn"):
        self.db, self.bin, self.unit, self.backup_dir = db, bin, unit, backup_dir

    def _cli(self, *args, timeout=30):
        r = subprocess.run([self.bin, *args, "-o", "json"], capture_output=True, text=True, timeout=timeout)
        if r.returncode:
            raise RuntimeError(f"headscale {args[0]} 실패: {(r.stderr or r.stdout).strip()[-200:]}")
        return json.loads(r.stdout or "null")

    def _systemctl(self, verb):
        return subprocess.run(["systemctl", verb, *(["--quiet"] if verb == "is-active" else []), self.unit],
                              timeout=60).returncode == 0

    def users(self):
        return [{"id": int(u["id"]), "name": u.get("name", "")} for u in self._cli("users", "list") or []]

    def nodes(self):
        out = []
        for n in self._cli("nodes", "list") or []:
            seen = n.get("last_seen") or n.get("lastSeen") or {}
            out.append({"id": int(n["id"]), "user_id": int((n.get("user") or {}).get("id", -1)),
                        "given_name": n.get("given_name") or n.get("givenName") or "", "hostname": n.get("name", ""),
                        "ip": _ip4(n), "online": bool(n.get("online")),
                        "last_seen": seen.get("seconds") if isinstance(seen, dict) else seen})
        return out

    def create_key(self, user_id, ttl="1h"):
        return self._cli("preauthkeys", "create", "--user", str(user_id), "--expiration", ttl)["key"]

    def delete_node(self, nid):
        self._cli("nodes", "delete", "--identifier", str(nid), "--force")

    def active(self):
        return self._systemctl("is-active")

    def ip_owner(self, ip, nid):
        con = sqlite3.connect(self.db, timeout=5)
        try:
            soft = any(c[1] == "deleted_at" for c in con.execute("PRAGMA table_info(nodes)"))
            r = con.execute("SELECT given_name FROM nodes WHERE ipv4=? AND id<>?" + (" AND deleted_at IS NULL" if soft else ""),
                            (ip, nid)).fetchone()
            return r[0] if r else None
        finally:
            con.close()

    def set_ipv4(self, nid, ip):
        """정지 → DB 백업 → 지운 노드가 쥔 같은 주소 비우기 → 갱신 → 시작. 실패해도 반드시 다시 시작."""
        if not self._systemctl("stop"):
            raise RuntimeError("headscale 정지 실패")
        try:
            bdir = os.path.join(self.backup_dir, time.strftime("%Y%m%d-%H%M%S"))
            os.makedirs(bdir, mode=0o700, exist_ok=True)
            shutil.copy2(self.db, bdir)
            con = sqlite3.connect(self.db, timeout=5)
            try:
                if any(c[1] == "deleted_at" for c in con.execute("PRAGMA table_info(nodes)")):
                    con.execute("UPDATE nodes SET ipv4=NULL WHERE ipv4=? AND id<>? AND deleted_at IS NOT NULL", (ip, nid))
                con.execute("UPDATE nodes SET ipv4=? WHERE id=?", (ip, nid))
                con.commit()
            finally:
                con.close()
        finally:
            self._systemctl("start")


def tailscale_paths():
    """이 서버의 tailscale 이 보는 각 노드 경로 — 직결(CurAddr) · 중계(Relay) · 대기(트래픽 없음)."""
    try:
        r = subprocess.run(["tailscale", "status", "--json"], capture_output=True, text=True, timeout=15)
        peers = (json.loads(r.stdout or "{}").get("Peer") or {}).values()
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return {}
    out = {}
    for p in peers:
        ip = next((a for a in p.get("TailscaleIPs") or [] if "." in a), "")
        out[ip] = (f"직결 {p['CurAddr']}" if p.get("CurAddr") else
                   f"중계 {p['Relay']}" if p.get("Active") and p.get("Relay") else "대기")
    return out
