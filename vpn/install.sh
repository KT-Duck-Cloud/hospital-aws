#!/usr/bin/env bash
# VPN 클라이언트 가입 — Ubuntu VM 에서 한 줄 (가입 화면이 /join/install.sh 로 내려 준다, 비밀값 없음)
#   curl -fsSL __LOGIN_SERVER__/join/install.sh | sudo bash -s -- t5-vm2
#     --ufw       VPN(tailscale0) · WireGuard 41641/udp 만 받기 — VPN 으로 SSH 되는 것을 확인한 뒤에
#     --dr-node   DR 컨트롤이 이 DB 노드를 다룰 운영 계정 drops (컨트롤 서버 주소에서 온 키만)
#   PL_JOIN_KEY_FILE=<파일>  키를 묻지 않고 파일에서 읽고 지움 (사람 없는 기동 — EC2 user-data 등)
# 다시 실행해도 안전 — 이미 맞는 주소로 가입돼 있으면 가입 단계를 건너뛴다
set -euo pipefail
LOGIN=__LOGIN_SERVER__
CTL=${PL_CONTROL_IP:-100.64.0.1}
say()  { printf '\033[1;34m[vpn]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[vpn]\033[0m %s\n' "$*"; }
die()  { printf '\033[1;31m[vpn]\033[0m %s\n' "$*" >&2; exit 1; }

HOST=${1:-}; shift || true
UFW=0; DRNODE=0
for a in "$@"; do case $a in --ufw) UFW=1 ;; --dr-node) DRNODE=1 ;; *) die "모르는 옵션 $a" ;; esac; done
[[ $HOST =~ ^t([1-9])-vm([1-9])$ ]] || die "호스트 이름은 t<번호>-vm<1~9> (예: t5-vm2)"
WANT=100.64.0.${BASH_REMATCH[1]}${BASH_REMATCH[2]}
[[ $EUID -eq 0 ]] || die "sudo 로 실행"
. /etc/os-release; [[ ${ID:-} == ubuntu ]] || die "Ubuntu 전용"
say "$HOST → $WANT"

hostnamectl set-hostname "$HOST"
command -v tailscale >/dev/null || curl -fsSL https://tailscale.com/install.sh | sh
systemctl enable --now tailscaled >/dev/null

cur() { tailscale ip -4 2>/dev/null | head -1 || true; }
if [[ $(cur) != "$WANT" ]]; then
  if [[ -n ${PL_JOIN_KEY_FILE:-} ]]; then
    KEY=$(tr -d '[:space:]' < "$PL_JOIN_KEY_FILE"); rm -f "$PL_JOIN_KEY_FILE"
  else
    read -rsp "가입 키 (화면에 보이지 않음): " KEY </dev/tty; echo
  fi
  [[ -n $KEY ]] || die "키가 비었음"
  KF=$(mktemp); chmod 600 "$KF"; printf '%s' "$KEY" > "$KF"; unset KEY; trap 'rm -f "$KF"' EXIT
  tailscale up --login-server "$LOGIN" --auth-key "file:$KF" --hostname "$HOST" --accept-dns=false --accept-routes=false --reset
  rm -f "$KF"
  say "주소 고정 대기 — 서버가 이름을 보고 $WANT 로 고친다 (최대 3분)"
  for i in $(seq 1 12); do
    sleep 15; [[ $(cur) == "$WANT" ]] && break
    systemctl restart tailscaled; sleep 5; [[ $(cur) == "$WANT" ]] && break
    printf '   지금 %s (%d/12)\n' "$(cur)" "$i"
  done
  [[ $(cur) == "$WANT" ]] || die "주소가 $WANT 가 아님 (지금 $(cur)) — 가입 화면에서 '주소 고정' 후 다시 실행"
fi
say "VPN 주소 $(cur)"

if (( DRNODE )); then
  PUB=$(curl -fsSL "$LOGIN/join/dr.pub" 2>/dev/null | head -1 || true)
  [[ $PUB =~ ^ssh-ed25519\ [A-Za-z0-9+/=]+(\ [[:graph:]]+)?$ ]] || die "DR 컨트롤 공개키를 받지 못함"
  id drops >/dev/null 2>&1 || useradd -m -s /bin/bash -c "pilotlight DR" drops
  install -d -m 700 -o drops -g drops /home/drops/.ssh
  printf 'from="%s",no-agent-forwarding,no-port-forwarding,no-X11-forwarding %s\n' "$CTL" "$PUB" > /home/drops/.ssh/authorized_keys
  chown drops:drops /home/drops/.ssh/authorized_keys; chmod 600 /home/drops/.ssh/authorized_keys
  echo 'drops ALL=(root) NOPASSWD: ALL' > /etc/sudoers.d/90-drops; chmod 440 /etc/sudoers.d/90-drops
  visudo -cqf /etc/sudoers.d/90-drops || { rm -f /etc/sudoers.d/90-drops; die "sudoers 검사 실패"; }
  command -v sshd >/dev/null || DEBIAN_FRONTEND=noninteractive apt-get install -y -qq openssh-server >/dev/null
  say "DR 운영 계정 drops — $CTL 에서 온 키만"
fi

sed -i -E "/^127\.0\.1\.1[[:space:]]/d; /[[:space:]]$HOST([[:space:]]|\$)/d" /etc/hosts
echo "$WANT $HOST" >> /etc/hosts
tailscale ping -c 3 "$CTL" | tail -1 || warn "컨트롤 서버 $CTL ping 실패 — 잠시 후 다시"

if (( UFW )); then
  ufw default deny incoming >/dev/null; ufw default allow outgoing >/dev/null
  ufw allow in on tailscale0 >/dev/null; ufw allow 41641/udp >/dev/null; ufw --force enable
  warn "이제 VPN 주소로만 접속: ssh <계정>@$WANT"
fi
say "완료 — $HOST $WANT"
