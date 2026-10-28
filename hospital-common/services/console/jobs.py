"""작업 — 한 번에 하나. 단계마다 (경과 초, 문구) 를 남겨 화면이 그대로 보여 준다. 자원 변경은 한 대씩: 끄기 → 변경 → 켜기 → 응답 확인."""
import threading
import time


class Jobs:
    def __init__(self, clock=time.time, sync=False, keep=20):
        self.now, self.sync, self.keep, self.list, self.lock = clock, sync, keep, [], threading.Lock()

    def running(self):
        return next((j for j in self.list if j["running"]), None)

    def start(self, kind, args, who, fn):
        with self.lock:
            if self.running():
                raise ValueError(f"진행 중인 작업이 있습니다: {self.running()['kind']}")
            job = {"id": int(self.now() * 1000), "kind": kind, "args": args, "who": who, "at": self.now(), "running": True,
                   "ok": None, "steps": []}
            self.list.insert(0, job)
            del self.list[self.keep:]

        def step(msg):
            job["steps"].append((round(self.now() - job["at"]), msg))

        def run():
            try:
                fn(step)
                job["ok"] = True
                step("완료")
            except Exception as e:
                job["ok"] = False
                step(f"실패: {e}")
            finally:
                job["running"] = False

        run() if self.sync else threading.Thread(target=run, daemon=True).start()
        return job


def resize(agent, health, sleep, name, cpus=None, mem_mb=None, gb=None, wait=300):
    """VM 사양 변경 — 보호 VM 은 에이전트가 거부한다. health(name) 가 True 가 될 때까지 기다린다."""
    def fn(step):
        vm = next((v for v in agent("vm_list")["vms"] if v["name"] == name), None)
        if not vm:
            raise ValueError(f"없는 VM: {name}")
        if vm["protected"]:
            raise ValueError(f"{name} 은 보호 대상")
        if vm["running"]:
            step(f"{name} 끄기")
            agent("vm_stop", name=name)
        if cpus or mem_mb:
            r = agent("vm_set", name=name, cpus=cpus or vm["cpus"], mem_mb=mem_mb or vm["mem_mb"])
            step(f"사양 {vm['cpus']} vCPU · {vm['mem_mb']}MB → {r['cpus']} vCPU · {r['mem_mb']}MB")
        if gb:
            r = agent("vm_disk_grow", name=name, gb=gb)
            step(f"디스크 {r['from_gb']} → {r['to_gb']}GB")
        step(f"{name} 켜기")
        agent("vm_start", name=name)
        t = 0
        while not health(name):
            if t >= wait:
                raise RuntimeError(f"{name} 켜졌지만 {wait}초 안에 응답 없음")
            sleep(5)
            t += 5
        step(f"{name} 응답 확인 ({t}초)")
    return fn


def power(agent, name, on):
    def fn(step):
        step(f"{name} {'켜기' if on else '끄기'}")
        r = agent("vm_start" if on else "vm_stop", name=name)
        step("이미 그 상태" if r.get("already") else "hard 종료" if r.get("hard") else "끝")
    return fn
