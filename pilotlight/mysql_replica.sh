#!/usr/bin/env bash
# AWS 대기 DB(vm2)에 MySQL 8.4 LTS 복제본 기반 설치 — 판정 서버가 SSH 로 root 실행. 여러 번 실행해도 안전
#   bash -s -- <server_id> < mysql_replica.sh
# 원본 연결 · 복제 계정 · 필터는 replica.py 가 SQL 로 넣는다 (여기에는 비밀값이 없다)
set -euo pipefail
SID=${1:?server_id}
[[ $SID =~ ^[0-9]+$ ]] || { echo "server_id 는 숫자"; exit 1; }
FP=BCA43417C3B485DD128EC6D4B7B3B788A8D3785C
say() { echo "[mysql-replica] $*"; }

have84() { command -v mysqld >/dev/null && mysqld --version | grep -qE ' 8\.4\.'; }
if ! have84; then
  command -v mysqld >/dev/null && { echo "이미 다른 MySQL: $(mysqld --version) — 중단"; exit 3; }
  [[ $(dpkg --print-architecture) == amd64 ]] || { echo "MySQL 8.4 공식 패키지는 amd64 만 — 중단"; exit 5; }
  say "MySQL 8.4 LTS 설치"
  export DEBIAN_FRONTEND=noninteractive
  command -v gpg >/dev/null || { apt-get update -qq; apt-get install -y -qq gpg curl ca-certificates >/dev/null; }
  K=$(mktemp); curl -fsSL https://repo.mysql.com/RPM-GPG-KEY-mysql-2025 -o "$K"
  gpg --show-keys --with-colons "$K" 2>/dev/null | grep -q "^fpr:::::::::$FP:" || { echo "서명 키 지문 불일치 — 중단"; exit 4; }
  install -d -m 755 /etc/apt/keyrings; gpg --dearmor --yes -o /etc/apt/keyrings/mysql.gpg "$K"; rm -f "$K"
  . /etc/os-release
  echo "deb [signed-by=/etc/apt/keyrings/mysql.gpg] http://repo.mysql.com/apt/ubuntu ${VERSION_CODENAME} mysql-8.4-lts" >/etc/apt/sources.list.d/mysql.list
  echo "mysql-community-server mysql-community-server/root-pass password" | debconf-set-selections
  echo "mysql-community-server mysql-community-server/re-root-pass password" | debconf-set-selections
  apt-get update -qq
  apt-get install -y -qq mysql-community-server >/dev/null
  have84 || { echo "설치 뒤에도 8.4 가 아님 — 중단"; exit 6; }
fi
say "$(mysqld --version)"

CNF=/etc/mysql/mysql.conf.d/90-pilotlight-replica.cnf
[[ -d /etc/mysql/mysql.conf.d ]] || CNF=/etc/mysql/conf.d/90-pilotlight-replica.cnf
NEW=$(mktemp)
cat >"$NEW" <<EOF
[mysqld]
server-id = $SID
gtid_mode = ON
enforce_gtid_consistency = ON
log_bin = mysql-bin
log_replica_updates = ON
binlog_expire_logs_seconds = 2592000
relay_log_recovery = ON
read_only = ON
super_read_only = ON
bind-address = 0.0.0.0
mysqlx = OFF
character-set-server = utf8mb4
collation-server = utf8mb4_0900_ai_ci
EOF

if cmp -s "$NEW" "$CNF" && mysql -N -e 'SELECT 1' >/dev/null 2>&1; then
  rm -f "$NEW"; say "설정 그대로 · 실행 중 — 재시작 안 함"
else
  install -m 644 "$NEW" "$CNF"; rm -f "$NEW"
  systemctl enable -q mysql 2>/dev/null || true
  systemctl restart mysql
fi
for i in $(seq 1 60); do mysql -N -e 'SELECT 1' >/dev/null 2>&1 && break; sleep 1; done
mysql -e 'SET GLOBAL super_read_only = ON'
mysql -N -e "SELECT CONCAT('ready server_id=', @@server_id, ' gtid=', @@gtid_mode, ' super_read_only=', @@super_read_only, ' version=', @@version)"
