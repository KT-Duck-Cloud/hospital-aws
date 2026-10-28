# AWS 조 앱 배치 (vm1, vm2 가 붙은 뒤) — 1조와 같은 구성으로 대기. common.sh 뒤에 붙어 실행된다
#   (REG · APP_TAG · GITOPS_REF · VALUES_B64 는 up.sh 가 앞에 붙인다 — 비밀값 아님)
#
#   1조 (KT-Duck-Cloud/hospital-gitops)                  2조 AWS (여기)
#   Traefik infra/traefik/traefik.yaml  NodePort 30080 → 같은 파일 (k3s 내장 Traefik 은 끔)
#   helm rsv · ns hospital · values.yaml (1조)          → helm rsv · ns hospital · values.yaml + deploy/aws/values-t8.yaml (0개 · 읽기 전용)
#   NodePort 30800 예약 · 30801 인증 · 30802 현황       → 차트 그대로 (같은 포트)
#   db-service → t3-vm2 :3306/hospital                 → db-service → t8-vm2 VPC 주소 :3306 (1조에서 GTID 복제 중인 복제본)
#   Secret 4종 (db-runtime · db-readonly · jwt · medical-key) → 역할 준비(provision.app_prepare)가 넣는다 — jwt · medical-key 는 1조 값 그대로 복사
#   승격 때 provision.app_up: --reuse-values --set readOnlyMode=false · 서비스마다 1개 → vm1:30800/readyz 200
#   이미 1조로 승격돼 운영 중이면(readOnlyMode=false) 다시 적용하지 않는다 — 운영 중인 앱을 0개로 되돌리지 않게
K="k3s kubectl"
export KUBECONFIG=/etc/rancher/k3s/k3s.yaml
SRC=/opt/duckcloud/hospital-gitops
CHART=$SRC/helm/reservation-app
VALUES=/opt/duckcloud/values-t8.yaml

say "1) 1조 저장소 hospital-gitops @ $GITOPS_REF"
command -v git >/dev/null || { apt-get update -qq; apt-get install -y -qq git >/dev/null; }
if [[ -d $SRC/.git ]]; then git -C "$SRC" fetch -q --depth 1 origin "$GITOPS_REF"; else
  mkdir -p /opt/duckcloud; git clone -q --depth 1 --branch "$GITOPS_REF" https://github.com/KT-Duck-Cloud/hospital-gitops "$SRC" \
    || { git clone -q https://github.com/KT-Duck-Cloud/hospital-gitops "$SRC"; git -C "$SRC" fetch -q origin "$GITOPS_REF"; }
fi
git -C "$SRC" -c advice.detachedHead=false checkout -q FETCH_HEAD 2>/dev/null || git -C "$SRC" -c advice.detachedHead=false checkout -q "$GITOPS_REF"
say "   $(git -C "$SRC" log -1 --format='%h %cd %s' --date=short)"
echo "$VALUES_B64" | base64 -d > "$VALUES"

say "2) Traefik — 1조와 같은 매니페스트 (NodePort 30080 · 대시보드 30088)"
# hospital-gitops 6087b11(10-17)이 infra/traefik 을 지움(운영 런북으로 옮김) — 없으면 지우기 직전 커밋(6373921)의 같은 파일
TRAEFIK=$SRC/infra/traefik/traefik.yaml
[[ -f $TRAEFIK ]] || { TRAEFIK=/opt/duckcloud/traefik.yaml
  curl -fsSL -o "$TRAEFIK" https://raw.githubusercontent.com/KT-Duck-Cloud/hospital-gitops/6373921/infra/traefik/traefik.yaml || die "Traefik 매니페스트를 받지 못함"; }
$K apply -f "$TRAEFIK" | sed 's/^/   /'
# 차트 Ingress 는 ingressClassName: traefik — 이 이름의 IngressClass 가 없으면 Traefik 이 Ingress 를 읽지 않아 모든 경로 404 (10-22 DR 승격 뒤)
$K apply -f - <<'YAML' | sed 's/^/   /'
apiVersion: networking.k8s.io/v1
kind: IngressClass
metadata:
  name: traefik
spec:
  controller: traefik.io/ingress-controller
YAML

say "3) 앱 이미지 — 1조와 같은 4개"; img_pull

say "4) namespace hospital · db-service → t8-vm2 (VPC)"
DBIP=$($K get node -l duckcloud/role=db -o jsonpath='{.items[0].status.addresses[?(@.type=="InternalIP")].address}')
[[ $DBIP =~ ^10\. ]] || die "DB 노드(라벨 duckcloud/role=db, t8-vm2) 주소를 못 찾음 — vm2.sh 먼저"
$K create namespace hospital --dry-run=client -o yaml | $K apply -f - >/dev/null
cat <<EOF | $K apply -f - | sed 's/^/   /'
apiVersion: v1
kind: Service
metadata: { name: db-service, namespace: hospital }
spec: { ports: [{ name: mysql, port: 3306, targetPort: 3306 }] }
---
apiVersion: v1
kind: Endpoints
metadata: { name: db-service, namespace: hospital }
subsets: [{ addresses: [{ ip: "$DBIP" }], ports: [{ name: mysql, port: 3306 }] }]
EOF

say "5) helm rsv (대기 — 0개 · READ_ONLY_MODE=true)"
RO=$(helm get values rsv -n hospital -a -o json 2>/dev/null | python3 -c 'import json,sys; print(str(json.load(sys.stdin).get("readOnlyMode","")).lower())' 2>/dev/null || true)
if [[ $RO == false ]]; then
  say "   지금 AWS 가 운영 중(readOnlyMode=false) — 다시 적용하지 않음"
else
  T=(--set "services.auth.image.tag=$APP_TAG" --set "services.reservation.image.tag=$APP_TAG"
     --set "services.stats.image.tag=$APP_TAG" --set "services.web.image.tag=$APP_TAG")
  # hospital-gitops 10-18: values-aws.yaml 이 values.yaml 하나로 합쳐짐 — 사이트 값은 values-t8.yaml 에만
  helm upgrade --install rsv "$CHART" -n hospital -f "$VALUES" "${T[@]}" | sed -n '1,6s/^/   /p'
fi

say "상태"
$K -n hospital get deploy --no-headers 2>/dev/null | awk '{print "   deploy", $1, $2}'
$K -n kube-system get svc traefik --no-headers 2>/dev/null | awk '{print "   traefik", $5}'
n=0; for s in reservation-app-db-runtime reservation-app-db-readonly reservation-app-jwt reservation-app-medical-key; do
  $K -n hospital get secret "$s" >/dev/null 2>&1 && n=$((n+1)); done
say "   Secret $n/4 $( ((n==4)) && echo '— 승격 준비 끝' || echo '— 운영 콘솔 메쉬 카드에서 AWS 역할 준비 승인 시 생김 (jwt · medical-key 는 1조 값 복사)')"
