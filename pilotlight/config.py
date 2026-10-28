import os
from dataclasses import dataclass


def _env(key, default, cast=str):
    v = os.environ.get(key)
    return default if v in (None, "") else cast(v)


def _hosts(v):
    return tuple(x.strip() for x in v.split(",") if x.strip())


@dataclass(frozen=True)
class Config:
    site1_dbs: tuple                # 앞쪽이 복귀 1순위
    site1_apps: tuple
    standby_db: str                 # AWS vm2 — 평시에도 켜 둠 (복제 수신)
    standby_app: str                # AWS vm1 — 평시 정지 (Pilot Light)
    db_name: str = "hospital"
    app_users: tuple = ("app_runtime", "app_readonly")
    repl_user: str = "repl_aws"
    standby_server_id: int = 182
    failover_sec: int = 300
    failback_sec: int = 180
    retry_sec: int = 600
    max_lag_sec: int = 60
    interval_sec: int = 30
    mode: str = "auto"
    app_port: int = 30080
    ready_path: str = "/readyz"
    ready_tries: int = 40
    state_path: str = ""
    secrets_path: str = ""
    backup_dir: str = "/var/backups/pilotlight"
    ssh_user: str = "drops"
    ssh_key: str = ""
    ssh_known_hosts: str = ""
    frontdoor_path: str = ""
    frontdoor_test: str = "nginx -t"
    frontdoor_reload: str = "systemctl reload nginx"
    aws_region: str = ""
    aws_app_name: str = "pilotlight-app"
    aws_db_name: str = "pilotlight-db"
    aws_asg: str = ""
    aws_workers: int = 2

    @property
    def auto(self):
        return self.mode == "auto"


def load():
    return Config(
        site1_dbs=_hosts(_env("DR_SITE1_DBS", "")),
        site1_apps=_hosts(_env("DR_SITE1_APPS", "")),
        standby_db=_env("DR_STANDBY_DB", ""),
        standby_app=_env("DR_STANDBY_APP", ""),
        db_name=_env("DR_DB_NAME", "hospital"),
        app_users=_hosts(_env("DR_APP_USERS", "app_runtime,app_readonly")),
        repl_user=_env("DR_REPL_USER", "repl_aws"),
        standby_server_id=_env("DR_STANDBY_SERVER_ID", 182, int),
        failover_sec=_env("DR_FAILOVER_SEC", 300, int),
        failback_sec=_env("DR_FAILBACK_SEC", 180, int),
        retry_sec=_env("DR_RETRY_SEC", 600, int),
        max_lag_sec=_env("DR_MAX_LAG_SEC", 60, int),
        interval_sec=_env("DR_INTERVAL_SEC", 30, int),
        mode=_env("DR_MODE", "auto"),
        app_port=_env("DR_APP_PORT", 30080, int),
        ready_path=_env("DR_READY_PATH", "/readyz"),
        state_path=_env("DR_STATE_PATH", "/var/lib/pilotlight/state.json"),
        secrets_path=_env("DR_SECRETS_PATH", "/etc/pilotlight/secrets.json"),
        backup_dir=_env("DR_BACKUP_DIR", "/var/backups/pilotlight"),
        ssh_user=_env("DR_SSH_USER", "drops"),
        ssh_key=_env("DR_SSH_KEY", "/etc/pilotlight/id_ed25519"),
        ssh_known_hosts=_env("DR_SSH_KNOWN_HOSTS", "/etc/pilotlight/known_hosts"),
        frontdoor_path=_env("FRONTDOOR_UPSTREAM_PATH", ""),
        frontdoor_test=_env("FRONTDOOR_TEST_CMD", "nginx -t"),
        frontdoor_reload=_env("FRONTDOOR_RELOAD_CMD", "systemctl reload nginx"),
        aws_region=_env("AWS_REGION", ""),
        aws_app_name=_env("AWS_APP_INSTANCE_NAME", "pilotlight-app"),
        aws_db_name=_env("AWS_DB_INSTANCE_NAME", "pilotlight-db"),
        aws_asg=_env("AWS_WORKER_ASG", ""),
        aws_workers=_env("AWS_WORKERS_ON_FAILOVER", 2, int),
    )
