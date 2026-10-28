"""hostagent — 호스트에서 root 로 VM 을 조작한다. 포트 없음: unix socket(root:console 0660) 으로 같은 호스트의 콘솔만 부른다.

  host_status                       호스트 CPU · 메모리 · 디스크
  vm_list                           설정에 등록된 VM 만 — 상태 · 사양 · 보호 여부
  vm_start {name} · vm_stop {name}  등록 VM 만, 끄기는 보호(protected) 아닌 것만 · soft 3분 → hard
  vm_set {name, cpus, mem_mb}       꺼진 VM 만, 한도 안, 256MB 단위, 늘릴 때 호스트 예비 메모리 확인
  vm_disk_grow {name, gb}           꺼진 VM 만, 늘리기만, 호스트 예비 디스크 확인
변경 동작은 LOCK 으로 한 번에 하나.
"""
import re
import threading
import time

NAME = re.compile(r"^[a-z0-9][a-z0-9.-]{0,40}$")


class HostAgent:
    def __init__(self, conf, vmm, sleep=time.sleep, log=print):
        self.c, self.v, self.sleep, self.log, self.lock = conf, vmm, sleep, log, threading.Lock()

    def entry(self, name, mutable=False):
        if not NAME.match(name or ""):
            raise ValueError("잘못된 VM 이름")
        e = self.c["vms"].get(name)
        if e is None:
            raise ValueError(f"등록되지 않은 VM: {name}")
        if mutable and e.get("protected"):
            raise ValueError(f"{name} 은 보호 대상입니다 ({e.get('role') or '운영'})")
        return e

    def off_or_fail(self, name, what):
        if name in self.v.running():
            raise ValueError(f"켜진 VM 은 {what} 수 없습니다 (먼저 끄기)")

    def host_status(self, a):
        return self.v.host()

    def vm_list(self, a):
        on = self.v.running()
        return {"vms": [{"name": n, "role": e.get("role", ""), "protected": bool(e.get("protected")), "running": n in on, **self.v.spec(n)}
                        for n, e in self.c["vms"].items()], "limits": self.c["limits"]}

    def vm_start(self, a):
        n = a.get("name")
        self.entry(n)
        with self.lock:
            if n in self.v.running():
                return {"started": True, "already": True}
            if not self.v.start(n):
                raise RuntimeError(f"{n} 시작 실패")
            for _ in range(20):
                if n in self.v.running():
                    return {"started": True}
                self.sleep(3)
            raise RuntimeError(f"{n}: 시작 명령은 끝났지만 켜지지 않음")

    def vm_stop(self, a):
        n = a.get("name")
        self.entry(n, mutable=True)
        with self.lock:
            if n not in self.v.running():
                return {"stopped": True, "already": True}
            self.v.stop(n)
            for _ in range(60):
                if n not in self.v.running():
                    return {"stopped": True}
                self.sleep(3)
            self.v.stop(n, hard=True)
            self.log(f"vm_stop {n}: soft 시간 초과 → hard")
            return {"stopped": n not in self.v.running(), "hard": True}

    def vm_set(self, a):
        n, lim = a.get("name"), self.c["limits"]
        self.entry(n, mutable=True)
        cpus, mem = int(a["cpus"]), int(a["mem_mb"])
        if not lim["cpus_min"] <= cpus <= lim["cpus_max"]:
            raise ValueError(f"vCPU 는 {lim['cpus_min']}~{lim['cpus_max']}")
        if not lim["mem_mb_min"] <= mem <= lim["mem_mb_max"] or mem % 256:
            raise ValueError(f"메모리는 {lim['mem_mb_min']}~{lim['mem_mb_max']}MB, 256MB 단위")
        with self.lock:
            self.off_or_fail(n, "바꿀")
            grow, avail = mem - self.v.spec(n)["mem_mb"], self.v.host()["mem_avail_mb"]
            if grow > 0 and avail - grow < lim["host_mem_reserve_mb"]:
                raise ValueError(f"호스트 여유 메모리 부족: 여유 {avail}MB · 증가 {grow}MB · 예비 {lim['host_mem_reserve_mb']}MB")
            self.v.set(n, {"numvcpus": cpus, "cpuid.coresPerSocket": cpus, "memsize": mem})
            self.log(f"vm_set {n}: cpus={cpus} mem={mem}")
            return {"cpus": cpus, "mem_mb": mem}

    def vm_disk_grow(self, a):
        n, lim, gb = a.get("name"), self.c["limits"], int(a["gb"])
        self.entry(n, mutable=True)
        with self.lock:
            self.off_or_fail(n, "늘릴")
            cur = self.v.disk_gb(n) or 0
            if gb <= cur:
                raise ValueError(f"늘리기만 가능합니다 (현재 {cur}GB)")
            if gb > lim["disk_gb_max"]:
                raise ValueError(f"디스크 최대 {lim['disk_gb_max']}GB")
            if self.v.host()["disk_free_gb"] - (gb - cur) < lim["host_disk_reserve_gb"]:
                raise ValueError("호스트 디스크 여유 부족")
            if not self.v.grow(n, gb):
                raise RuntimeError("디스크 확장 실패")
            return {"from_gb": cur, "to_gb": gb, "note": "부팅할 때 cloud-init growpart 가 파티션을 늘린다"}

    def ops(self):
        return {k: getattr(self, k) for k in ("host_status", "vm_list", "vm_start", "vm_stop", "vm_set", "vm_disk_grow")}
