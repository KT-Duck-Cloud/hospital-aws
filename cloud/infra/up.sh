#!/usr/bin/env bash
# AWS 조 켜기 — 개발 · 시험할 때만. 끝나면 down.sh (평소 꺼 둠, 과금 OFF)
#   bash cloud/infra/up.sh                vm1 · vm2 켜고 설정(SSM Run Command, SSH 없음)
#   bash cloud/infra/up.sh --workers 1    + 추가 워커 1대 (최대 2)
#   bash cloud/infra/up.sh --no-setup     켜기만 (설정은 이미 끝난 경우 — 더 빠름)
# 켜는 동안 요금: t3.small 1대 시간당 약 $0.026 + 공인 IPv4 $0.005
source "$(dirname "$0")/lib.sh"
WORKERS=""; SETUP=1
while (( $# )); do
  case $1 in
    --workers) WORKERS=${2:?}; shift ;;
    --no-setup) SETUP=0 ;;
    *) die "모르는 인자: $1" ;;
  esac; shift
done
[[ -z $WORKERS || $WORKERS =~ ^[0-2]$ ]] || die "--workers 는 0~2"
VM1=$(out DrControlInstanceId); VM2=$(out DbInstanceId); ASG=$(out DrAsgName); BUCKET=$(out ArtifactBucketName)
# 앱 = 1조와 같은 차트 · 이미지 (hospital-gitops @ $GITOPS_REF, $REG/*:$APP_TAG). Helm 값은 site-values/values-t8.yaml
VALUES_B64=$(base64 < "$HERE/site-values/values-t8.yaml" | tr -d '\n')
KSM_B64=$(base64 < "$HERE/site-values/kube-state-metrics.yaml" | tr -d '\n')   # 1조와 같은 kube-state-metrics (secrets 제외) — vm1.sh 6
[[ -n $VM2 && $VM2 != None ]] || die "vm2 가 아직 없음 — bash cloud/infra/deploy.sh --apply 먼저"

log "켜기 vm1 $VM1 · vm2 $VM2"
aws ec2 start-instances --instance-ids "$VM1" "$VM2" >/dev/null
aws ec2 wait instance-running --instance-ids "$VM1" "$VM2"
log "SSM 에이전트 연결 대기"
for i in $(seq 1 40); do
  n=$(aws ssm describe-instance-information --filters "Key=InstanceIds,Values=$VM1,$VM2" \
        --query "length(InstanceInformationList[?PingStatus=='Online'])" --output text)
  [[ $n == 2 ]] && break; sleep 6
done
[[ $n == 2 ]] || die "SSM 연결이 안 됨 (Online $n/2) — 잠시 후 다시"

run_node() {  # run_node <instance> <script>   (REG · APP_TAG · GITOPS_REF · VALUES_B64 를 앞에 붙여 보낸다 — 비밀값 아님)
  local id=$1 f=$2 j cid st
  j=$(mktemp)
  PRE="BUCKET=$BUCKET IMG_TAG=$IMG_TAG REG=$REG APP_TAG=$APP_TAG GITOPS_REF=$GITOPS_REF VALUES_B64=${VALUES_B64} KSM_B64=${KSM_B64}" \
  python3 - "$HERE/node/common.sh" "$HERE/node/$f" > "$j" <<'PY'
import base64, json, os, sys
src = "#!/usr/bin/env bash\n" + os.environ["PRE"] + "\n" + "".join(open(p).read() + "\n" for p in sys.argv[1:])
b = base64.b64encode(src.encode()).decode()
print(json.dumps({"commands": [f"echo {b} | base64 -d > /root/duck-node.sh", "bash /root/duck-node.sh"],
                  "executionTimeout": ["1800"]}))
PY
  cid=$(aws ssm send-command --instance-ids "$id" --document-name AWS-RunShellScript \
          --comment "duckcloud $f" --parameters "file://$j" --query Command.CommandId --output text)
  rm -f "$j"
  log "$f 실행 중 ($id)…"
  for i in $(seq 1 180); do
    st=$(aws ssm get-command-invocation --command-id "$cid" --instance-id "$id" --query Status --output text 2>/dev/null || echo Pending)
    [[ $st == Pending || $st == InProgress || $st == Delayed ]] || break; sleep 10
  done
  aws ssm get-command-invocation --command-id "$cid" --instance-id "$id" \
    --query '[StandardOutputContent,StandardErrorContent]' --output text | grep -v '^None$' | tail -40
  [[ $st == Success ]] || die "$f 실패 ($st)"
}

if (( SETUP )); then
  run_node "$VM1" vm1.sh
  run_node "$VM2" vm2.sh
  run_node "$VM1" app.sh
  run_node "$VM1" users.sh   # 팀원 접속 계정 duck-admin · duck-view (Session Manager Run As, session.sh)
  run_node "$VM2" users.sh
  if aws ssm get-parameter --name "$PARAM/join-key" >/dev/null 2>&1; then
    aws ssm delete-parameter --name "$PARAM/join-key" && log "가입 키 지움 (두 노드 가입 끝)"
  fi
fi
if [[ -n $WORKERS ]]; then
  aws autoscaling set-desired-capacity --auto-scaling-group-name "$ASG" --desired-capacity "$WORKERS"
  log "추가 워커 $WORKERS 대 요청 — 2~3분 뒤 vm1 k3s 에 붙음"
fi
log "켜짐 — vm1 $(state "$VM1") · vm2 $(state "$VM2") · 지금 AWS 조 역할: $(aws_role)"
warn "작업이 끝나면: bash cloud/infra/down.sh"
