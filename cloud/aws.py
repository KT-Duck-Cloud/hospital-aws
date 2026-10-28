"""AWS 호출 창구 — 화면 API 는 여기만 부른다. 호출마다 (성공, JSON, 오류) 로 돌려주고, API 별 횟수 · 실패 · 지연을 남긴다 (연결 화면).
이 저장소는 실제 계정에 붙지 않는다: run 은 데모(cloud/demo.py)가 채운다."""
import json
import threading
import time


class Aws:
    def __init__(self, profile="demo", region="ap-southeast-2"):
        self.profile, self.region = profile, region
        self.stats, self.session, self.lock = {}, {"ok": True, "error": "", "checked": 0.0}, threading.Lock()

    def run(self, args, region=None, timeout=15):
        raise NotImplementedError("데모 전용 — cloud.demo.DemoAws 를 쓰세요")

    def __call__(self, args, region=None, timeout=15):
        t0 = time.time()
        ok, out, err = self.run(args, region, timeout)
        data = None
        if ok:
            try:
                data = json.loads(out) if out.strip() else {}
            except ValueError:
                ok, err = False, "JSON 아님"
        with self.lock:
            s = self.stats.setdefault(" ".join(args[:2]), {"api": " ".join(args[:2]), "count": 0, "fail": 0})
            s.update(count=s["count"] + 1, fail=s["fail"] + (not ok), last_ok=ok, last_at=time.strftime("%H:%M:%S"),
                     last_ms=int((time.time() - t0) * 1000), last_error=(err or "")[-200:] if not ok else "")
            self.session.update(ok=True, checked=time.time())
        return ok, data, (err or "")
