from . import config, cost, execs, fence, flows, frontdoor, judge, power, replica, runner, store  # noqa: F401


def build(cfg, clock=None, probe=None, ex=None, pw=None, door=None, sync=False):
    clock = clock or execs.Clock()
    return flows.Ctx(cfg=cfg, clock=clock, probe=probe or execs.Probe(),
                     ex=ex or execs.SSHExec(cfg.ssh_user, cfg.ssh_key, cfg.ssh_known_hosts),
                     power=pw or power.Ec2Power(cfg),
                     door=door or frontdoor.Frontdoor(cfg.frontdoor_path, cfg.app_port, cfg.frontdoor_test, cfg.frontdoor_reload),
                     store=store.Store(cfg.state_path), secrets=store.Secrets(cfg.secrets_path), runner=runner.Runner(clock, sync))
