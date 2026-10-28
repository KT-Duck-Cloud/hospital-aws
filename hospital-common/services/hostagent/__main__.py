"""python -m services.hostagent  (root) — HOSTAGENT_CONF · HOSTAGENT_SOCK · HOSTAGENT_GROUP"""
import grp
import json
import os
import time

from common import rpc
from services.hostagent.agent import HostAgent
from services.hostagent.vmm import Vmm


def main():
    env = os.environ.get
    with open(env("HOSTAGENT_CONF", "/etc/hospital-common/hostagent.json")) as f:
        conf = json.load(f)
    sock, gid = env("HOSTAGENT_SOCK", "/run/hostagent/agent.sock"), grp.getgrnam(env("HOSTAGENT_GROUP", "console")).gr_gid
    os.makedirs(os.path.dirname(sock), exist_ok=True)
    os.chown(os.path.dirname(sock), 0, gid)
    os.chmod(os.path.dirname(sock), 0o750)
    log = lambda m: print(time.strftime("%FT%T"), m, flush=True)  # noqa: E731
    rpc.serve(sock, HostAgent(conf, Vmm(), log=log).ops(), 0o660, gid, log)
    log(f"hostagent {sock}")
    while True:
        time.sleep(3600)


if __name__ == "__main__":
    main()
