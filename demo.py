"""로컬 데모 — 가상 서버를 모두 띄운다. 실제 서버 · AWS 계정 없이 이 컴퓨터 안에서만 돈다. Ctrl-C 로 모두 끝.

  python3 demo.py

  [dr]  python3 -m sim.stack --manual          DR 컨트롤 · VPN 에이전트 · 가입 화면 · 통합 관제(서비스 상태 · DR 현황 · 관리 · AWS 데모)
        가짜 1조 t3 · t5 와 AWS t8 (MySQL · Headscale · EC2) 가 한 프로세스 안에서 돈다
  [ops] hospital-common: python3 -m sim.stack 게이트웨이 S1 · S2 · Headscale 주/대기 · 호스트 VM · 운영 콘솔 · 공개 첫 화면
"""
import os
import subprocess
import sys
import threading
import time

ROOT = os.path.dirname(os.path.abspath(__file__))
PARTS = [("dr", ROOT, ["-m", "sim.stack", "--manual"]), ("ops", os.path.join(ROOT, "hospital-common"), ["-m", "sim.stack"])]


def pump(tag, p):
    for line in p.stdout:
        print(f"[{tag}] {line.rstrip()}", flush=True)


def main():
    if sys.version_info < (3, 12):
        sys.exit("Python 3.12 이상이 필요합니다")
    procs = []
    for tag, cwd, args in PARTS:
        p = subprocess.Popen([sys.executable, "-u", *args], cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                             env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
        threading.Thread(target=pump, args=(tag, p), daemon=True).start()
        procs.append(p)
    time.sleep(3)
    print("\n열어 볼 곳 — 서비스 상태 http://127.0.0.1:8095/ · DR 현황 /dr · 관리 /admin · AWS 데모 /aws/ · 가입 http://127.0.0.1:8096/join"
          " · 운영 콘솔 http://127.0.0.1:8190/ · 첫 화면 http://127.0.0.1:8191/\n", flush=True)
    try:
        while all(p.poll() is None for p in procs):
            time.sleep(1)
        print("한 서버가 멈췄습니다 — 나머지도 끝냅니다", flush=True)
    except KeyboardInterrupt:
        pass
    for p in procs:
        p.terminate()
    for p in procs:
        p.wait(10)


if __name__ == "__main__":
    main()
