# AWS 조 vm1 (t8-vm1 100.64.0.81) — DR컨트롤 + k3s 서버(마스터). common.sh 뒤에 붙어 실행된다
say "1) 0.1 컨테이너 정리"; legacy_off

say "2) VPN"; vpn_join t8-vm1 100.64.0.81

say "3) k3s 서버 $K3S_VERSION (노드 주소 = VPC $PRIV, API 는 VPN 100.64.0.81 로도)"
TOKEN=$(pget k3s-token 1 || true)
if [[ -z $TOKEN || $TOKEN == None ]]; then
  TOKEN=$(head -c 32 /dev/urandom | od -An -tx1 | tr -d ' \n')
  tf=$(mktemp); chmod 600 "$tf"; printf '%s' "$TOKEN" > "$tf"
  aws ssm put-parameter --region "$REGION" --name /duckcloud/aws/k3s-token --type SecureString --value "file://$tf" --overwrite >/dev/null
  rm -f "$tf"; say "   k3s 토큰 새로 만듦 → SSM (값은 출력하지 않음)"
fi
if ! k3s_ok k3s; then   # 내장 Traefik 은 끈다 — 1조와 같은 매니페스트(hospital-gitops infra/traefik, NodePort 30080)를 app.sh 가 넣는다
  curl -sfL https://get.k3s.io | INSTALL_K3S_VERSION="$K3S_VERSION" K3S_TOKEN="$TOKEN" sh -s - server \
    --node-ip "$PRIV" --tls-san 100.64.0.81 --disable traefik --write-kubeconfig-mode 600 \
    --node-label duckcloud/role=master >/dev/null
fi
unset TOKEN
uf=$(mktemp); printf 'https://%s:6443' "$PRIV" > "$uf"     # 옛 awscli v1 은 --value 의 https:// 를 내려받으려 한다 — 파일로
aws ssm put-parameter --region "$REGION" --name /duckcloud/aws/k3s-url --type String --value "file://$uf" --overwrite >/dev/null
rm -f "$uf"
for i in $(seq 1 30); do k3s kubectl get --raw /readyz >/dev/null 2>&1 && break; sleep 4; done

say "4) helm (1조와 같은 차트를 쓰려고)"; helm_install

say "5) kube-state-metrics — 1조와 같게 namespace monitoring · NodePort 30810 (secrets 제외, deploy/aws/kube-state-metrics.yaml)"
if [[ -n ${KSM_B64:-} ]]; then
  echo "$KSM_B64" | base64 -d > /opt/duckcloud-ksm.yaml
  k3s kubectl apply -f /opt/duckcloud-ksm.yaml | sed 's/^/   /'
  k3s kubectl -n monitoring rollout status deploy/kube-state-metrics --timeout=180s | sed 's/^/   /' || say "   기동 확인 실패 — k3s kubectl -n monitoring get pods"
fi

say "6) 판정 · 승격은 ctl duck-hsagent (또는 dr-ctl t7-vm1) — 이 VM 은 AWS 앱 사이트 (앱 배치는 app.sh)"

say "상태"
tailscale ip -4 | head -1
k3s kubectl get nodes -o wide --no-headers 2>/dev/null | awk '{print "   node", $1, $2, $6}'
free -m | awk '/Mem/{printf "   메모리 %d/%d MB\n",$3,$2}'
