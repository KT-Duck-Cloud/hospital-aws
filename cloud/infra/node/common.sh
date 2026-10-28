# AWS 조 노드 공통 — up.sh 가 vm1.sh · vm2.sh 앞에 붙여 SSM Run Command(root)로 보낸다. 여러 번 실행해도 안전
set -euo pipefail
LOGIN=https://vpn.example.org:63728
K3S_VERSION=v1.31.4+k3s1
say()  { printf '[duck-aws %s] %s\n' "$(hostname)" "$*"; }
die()  { printf '[duck-aws %s] 실패: %s\n' "$(hostname)" "$*" >&2; exit 1; }

T=$(curl -fsS -X PUT http://169.254.169.254/latest/api/token -H 'X-aws-ec2-metadata-token-ttl-seconds: 600')
md() { curl -fsS -H "X-aws-ec2-metadata-token: $T" "http://169.254.169.254/latest/meta-data/$1"; }
REGION=$(md placement/region); PRIV=$(md local-ipv4)

command -v aws >/dev/null || { export DEBIAN_FRONTEND=noninteractive; apt-get update -qq; apt-get install -y -qq awscli >/dev/null; }
pget() { aws ssm get-parameter --region "$REGION" --name "/duckcloud/aws/$1" ${2:+--with-decryption} --query Parameter.Value --output text 2>/dev/null; }

# 0.1 시절 컨테이너(Headscale · PostgreSQL · MSSQL · 콘솔)는 2GB 메모리를 다 쓰고 지금 구조에 없다 — 멈추고 자동 시작만 끈다(지우지 않음)
legacy_off() {
  command -v docker >/dev/null || return 0
  local ids; ids=$(docker ps -q)
  [[ -z $ids ]] && return 0
  say "0.1 컨테이너 멈춤 (데이터 · 이미지는 그대로): $(docker ps --format '{{.Names}}' | tr '\n' ' ')"
  docker update --restart=no $ids >/dev/null; docker stop $ids >/dev/null
}

# VPN 가입 — 팀원 VM 과 같은 install.sh (duckops 관리 계정 · node-exporter · 주소 고정 대기). 가입 키는 SSM 에서 한 번만
vpn_join() {  # vpn_join t8-vm1 100.64.0.81
  local host=$1 want=$2
  if [[ $(tailscale ip -4 2>/dev/null | head -1) == "$want" ]]; then
    say "VPN $host $want — 이미 가입됨"
    curl -fsSL "$LOGIN/join/install.sh" | bash -s -- "$host"     # duckops · node-exporter 갱신 (가입 단계는 건너뜀)
    return
  fi
  local kf; kf=$(mktemp); chmod 600 "$kf"
  pget join-key 1 > "$kf" || { rm -f "$kf"; die "가입 키가 없습니다 — Mac 에서 cloud/infra/put-join-key.sh 먼저"; }
  curl -fsSL "$LOGIN/join/install.sh" | DUCK_JOIN_KEY_FILE="$kf" bash -s -- "$host"
  rm -f "$kf"
  [[ $(tailscale ip -4 | head -1) == "$want" ]] || die "VPN 주소가 $want 가 아님"
}

k3s_ok() { systemctl is-active --quiet "$1" && [[ $(k3s --version 2>/dev/null | head -1) == *"${K3S_VERSION}"* ]]; }

# 1조와 같은 앱 이미지 4개 — 1조 레지스트리(REG, VPN)에서 받아 k3s containerd 에 넣고 고정(pinned: 쓰지 않는 동안 GC 로 지워지지 않게).
#   REG · APP_TAG 는 up.sh 가 앞에 붙인다. 레지스트리에 닿지 않으면 이미 받아 둔 것을 그대로 쓴다 (DR 때 1조 쪽이 다 꺼져 있어도 뜨게)
APP_IMAGES="reservation-app-auth reservation-app-reservation reservation-app-stats reservation-app-web"
img_pull() {
  local n ref ok=0 miss=0
  for n in $APP_IMAGES; do
    ref="$REG/$n:$APP_TAG"
    if k3s ctr -n k8s.io images pull --plain-http "$ref" >/dev/null 2>&1; then ok=$((ok+1))
    elif k3s ctr -n k8s.io images ls -q | grep -qx "$ref"; then say "   $n — 레지스트리에 닿지 않음, 받아 둔 이미지 사용"
    else miss=$((miss+1)); say "   $n — 받지 못함 ($ref)"; continue; fi
    k3s ctr -n k8s.io images label "$ref" io.cri-containerd.pinned=pinned >/dev/null 2>&1 || true
  done
  say "   이미지 $REG/*:$APP_TAG — 새로 받음 $ok · 없음 $miss (고정됨)"
  k3s ctr -n k8s.io images ls 2>/dev/null | awk -v r="$REG/" 'index($1,r)==1{print "   " $1, $3}' | head -8
  (( miss == 0 )) || die "앱 이미지 $miss 개가 없음 — 1조 레지스트리 $REG 가 VPN 에서 닿는지 (ACL t8 → 100.64.0.41:8086)"
}

HELM_VERSION=v3.16.4
helm_install() {
  command -v helm >/dev/null && [[ $(helm version --short 2>/dev/null) == "$HELM_VERSION"* ]] && return
  local t; t=$(mktemp -d)
  curl -fsSL "https://get.helm.sh/helm-$HELM_VERSION-linux-amd64.tar.gz" -o "$t/h.tgz"
  curl -fsSL "https://get.helm.sh/helm-$HELM_VERSION-linux-amd64.tar.gz.sha256sum" -o "$t/h.sha"
  (cd "$t" && echo "$(cut -d' ' -f1 h.sha)  h.tgz" | sha256sum -c - >/dev/null) || die "helm 내려받기 검증 실패"
  tar -xzf "$t/h.tgz" -C "$t" && install -m 0755 "$t/linux-amd64/helm" /usr/local/bin/helm; rm -rf "$t"
  say "   helm $(helm version --short)"
}

# 옛 방식 (초기이미지 9e8e706 tar, deploy/k8s/site-standby-app) — 2026-10-16 부터 쓰지 않음. 아래 img_import 는 기록용
# 앱 이미지 — S3(VPC 게이트웨이)에서 tar 를 받아 k3s containerd 에 (이미 있으면 건너뜀). BUCKET · IMG_TAG 는 up.sh 가 앞에 붙인다
img_import() {
  local tar=/var/tmp/app-$IMG_TAG.tar
  if k3s ctr -n k8s.io images ls -q 2>/dev/null | grep -q "reservation-app-reservation:$IMG_TAG$"; then
    say "   이미지 $IMG_TAG — 이미 있음"; return
  fi
  aws s3 cp --region "$REGION" --only-show-errors "s3://$BUCKET/images/app-$IMG_TAG.tar" "$tar" \
    || die "이미지 tar 없음 — Mac 에서 bash cloud/infra/put-image.sh 먼저"
  k3s ctr -n k8s.io images import "$tar" >/dev/null; rm -f "$tar"
  k3s ctr -n k8s.io images ls -q | grep "reservation-app-.*:$IMG_TAG$" | sed 's/^/   /'
}
