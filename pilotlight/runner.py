import threading


class Runner:
    """한 번에 하나 — 준비 · 승격 · 복귀가 겹치지 않게. fn(step) → dict, done(name, ok, result)."""

    def __init__(self, clock, sync=False):
        self.clock, self.sync = clock, sync
        self.lock = threading.Lock()
        self.job, self.jobs = {"running": False}, []

    @property
    def busy(self):
        return self.job.get("running")

    def start(self, name, fn, done):
        with self.lock:
            if self.job.get("running"):
                raise ValueError(f"이미 진행 중: {self.job.get('name')}")
            self.job = {"running": True, "name": name, "started": self.clock.now(), "steps": [], "ok": None}
            self.jobs = (self.jobs + [self.job])[-10:]
        job = self.job

        def step(msg):
            job["steps"].append([round(self.clock.now() - job["started"]), msg])

        def body():
            try:
                res = fn(step) or {}
                job["ok"] = True
            except Exception as e:
                step(f"실패: {e}")
                job["ok"], res = False, {"error": str(e)}
            finally:
                job["running"], job["ended"] = False, self.clock.now()
            done(name, job["ok"], res)

        if self.sync:
            body()
        else:
            threading.Thread(target=body, daemon=True).start()
        return {"started": True, "name": name}
