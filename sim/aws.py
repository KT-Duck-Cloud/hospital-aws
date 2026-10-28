"""데모 AWS 를 가짜 인프라(cluster.py)와 한 몸으로 — vm1(앱) 켜짐 = cluster.apps[AWS_APP] (DR 승격 · 복귀가 바꾼다),
vm2(DB) 켜짐 = AWS DB 서버 up, 워커 = DR 이 마지막으로 늘린 수. 추가한 VM 은 데모 쪽에만 있다."""
from cloud.demo import DemoAws
from sim import scenario as sc


class FakeAws(DemoAws):
    def __init__(self, cluster, **kw):
        super().__init__(**kw)
        self.c = cluster

    def on(self, key):
        if key == "vm1":
            return bool(self.c.apps.get(sc.AWS_APP))
        if key == "vm2":
            return self.c.servers[sc.AWS_DB].up
        return super().on(key)

    def set(self, key, up):
        if key == "vm1":
            self.c.apps[sc.AWS_APP] = up
        elif key == "vm2":
            self.c.servers[sc.AWS_DB].up = up
        else:
            super().set(key, up)

    def scale(self, n):
        self.c.scaled.append(n)

    def workers(self):
        return (self.c.scaled or [0])[-1]
