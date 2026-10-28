import argparse
import json
import os
import time

from . import build, config, control, cost, flows, judge


def main():
    ap = argparse.ArgumentParser(prog="python -m pilotlight")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("run", help="판정 루프 (interval 초마다)")
    sub.add_parser("once", help="판정 1회 · 상태 출력")
    p = sub.add_parser("prepare", help="AWS 대기 DB 를 1조 DB 복제본으로 준비")
    p.add_argument("--source")
    p.add_argument("--force", action="store_true")
    sub.add_parser("cost", help="대기 사이트 월 비용 비교")
    a = ap.parse_args()

    if a.cmd == "cost":
        print(cost.table())
        return
    cfg = config.load()
    if not (cfg.site1_dbs and cfg.standby_db and cfg.standby_app):
        ap.error("DR_SITE1_DBS · DR_STANDBY_DB · DR_STANDBY_APP 가 필요합니다 (.env.example)")
    ctx = build(cfg, sync=a.cmd != "run")
    j = judge.Judge(ctx)
    if a.cmd == "prepare":
        flows.prepare(ctx, a.source, a.force)
        print(json.dumps(ctx.runner.job, ensure_ascii=False, indent=1))
    elif a.cmd == "once":
        j.tick()
        print(json.dumps({"dr": ctx.store.dr, "repl": ctx.store.data["repl"], "history": ctx.store.data["history"][:10]},
                         ensure_ascii=False, indent=1))
    else:
        flows.door(ctx)
        listen, token = os.environ.get("DR_CONTROL_LISTEN", ""), os.environ.get("DR_CONTROL_TOKEN", "")
        if listen:
            if len(token) < 32:
                ap.error("DR_CONTROL_TOKEN 은 32자 이상 (openssl rand -hex 32)")
            control.serve(ctx, j, listen, token)
            print("DR 컨트롤 API", listen, flush=True)
        while True:
            try:
                print(time.strftime("%H:%M:%S"), j.tick(), flush=True)
            except Exception as e:
                print(time.strftime("%H:%M:%S"), "판정 실패", e, flush=True)
            time.sleep(cfg.interval_sec)


if __name__ == "__main__":
    main()
