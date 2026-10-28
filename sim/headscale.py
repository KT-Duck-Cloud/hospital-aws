"""가짜 Headscale — vpn.headscale.Headscale 과 같은 메서드. 키는 1회용 · 만료, 가입하면 임의 주소(.100~)를 받는다."""
import secrets


class FakeHeadscale:
    def __init__(self, clock, online=lambda ip: True):
        self.clock, self.online = clock, online
        self._users, self._nodes, self.keys, self.running, self.backups = [], [], {}, True, 0
        self._next_id, self._next_ip = 1, 100

    def add_user(self, name):
        self._users.append({"id": len(self._users) + 1, "name": name})
        return self._users[-1]["id"]

    def users(self):
        return [dict(u) for u in self._users]

    def nodes(self):
        return [{**n, "online": n["online"] and self.online(n["ip"])} for n in self._nodes if not n.get("deleted")]

    def create_key(self, user_id, ttl="1h"):
        k = secrets.token_hex(24)
        self.keys[k] = {"user_id": user_id, "exp": self.clock() + 3600, "used": False}
        return k

    def register(self, key, hostname):
        """tailscale up --auth-key … --hostname … 에 해당."""
        k = self.keys.get(key)
        if not k or k["used"] or k["exp"] < self.clock():
            raise PermissionError("invalid or expired auth key")
        k["used"] = True
        ip = f"100.64.0.{self._next_ip}"
        self._next_ip += 1
        self._nodes.append({"id": self._next_id, "user_id": k["user_id"], "given_name": hostname, "hostname": hostname,
                            "ip": ip, "online": True, "last_seen": self.clock()})
        self._next_id += 1
        return ip

    def delete_node(self, nid):
        for n in self._nodes:
            if n["id"] == nid:
                n["deleted"] = True

    def active(self):
        return self.running

    def ip_owner(self, ip, nid):
        return next((n["given_name"] for n in self._nodes if n["ip"] == ip and n["id"] != nid and not n.get("deleted")), None)

    def set_ipv4(self, nid, ip):
        self.backups += 1
        for n in self._nodes:
            if n["id"] == nid:
                n["ip"] = ip

    def ip_of(self, hostname):
        return next(n["ip"] for n in self._nodes if n["given_name"] == hostname and not n.get("deleted"))
