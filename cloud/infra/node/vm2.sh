# AWS 조 vm2 (t8-vm2 100.64.0.82) — MySQL 8.4 복제본 + k3s 에이전트(워커). common.sh 뒤에 붙어 실행된다
#   MySQL 은 여기서 깔지 않는다 — 팀원 vm2 와 같이 ctl 메쉬의 '역할 준비'(provision.py → mysql_replica.sh, duckops)가 넣는다
say "1) VPN"; vpn_join t8-vm2 100.64.0.82

say "2) k3s 에이전트 $K3S_VERSION → vm1"
URL=$(pget k3s-url) || die "k3s-url 없음 — vm1 먼저"
if ! k3s_ok k3s-agent; then
  TOKEN=$(pget k3s-token 1) || die "k3s-token 없음 — vm1 먼저"
  curl -sfL https://get.k3s.io | INSTALL_K3S_VERSION="$K3S_VERSION" K3S_URL="$URL" K3S_TOKEN="$TOKEN" sh -s - agent \
    --node-ip "$PRIV" --node-label duckcloud/role=db >/dev/null
  unset TOKEN
fi

say "상태"
tailscale ip -4 | head -1
echo "   k3s-agent $(systemctl is-active k3s-agent || true)"
command -v mysqld >/dev/null && mysqld --version | sed 's/^/   /' || say "   MySQL 아직 없음 — 운영 콘솔 메쉬 카드에서 AWS 조 '역할 준비' 승인 시 설치"
free -m | awk '/Mem/{printf "   메모리 %d/%d MB\n",$3,$2}'

say "3) 앱 이미지 — 1조와 같은 4개 (파드가 vm2 에도 뜰 수 있게)"; img_pull
