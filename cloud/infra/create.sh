#!/usr/bin/env bash
# 새 프로젝트에 처음 한 번 — 스택 duckcloud-network(network.yaml) → duckcloud-compute(compute.yaml)
#   AWS_PROFILE=duck-new bash cloud/infra/create.sh            미리보기 (프로젝트 · 리전 · AMI · 만들 스택)
#   AWS_PROFILE=duck-new bash cloud/infra/create.sh --apply    만들기 — vm1 · vm2 는 켜진 채로 생김 → up.sh 또는 down.sh
# 이미 있는 스택은 건드리지 않는다 (바꾸는 건 deploy.sh). AMI 는 Canonical 공개 파라미터의 Ubuntu 22.04 (amd64)
source "$(dirname "$0")/lib.sh"
APPLY=0; [[ ${1:-} == --apply ]] && APPLY=1
NET=duckcloud-network
AMI_PARAM=/aws/service/canonical/ubuntu/server/22.04/stable/current/amd64/hvm/ebs-gp2/ami-id
ACCT=$(aws sts get-caller-identity --query Account --output text)
exists() { aws cloudformation describe-stacks --stack-name "$1" >/dev/null 2>&1; }
AMI=$(aws ssm get-parameter --name "$AMI_PARAM" --query Parameter.Value --output text)
log "프로젝트 ${ACCT:0:4}…${ACCT: -4} · 프로필 $AWS_PROFILE · 리전 $AWS_REGION · AMI $AMI"
for s in "$NET" "$STACK"; do exists "$s" && log "$s 있음 — 건너뜀" || log "$s 만들 예정"; done
aws cloudformation validate-template --template-body "file://$HERE/network.yaml" >/dev/null
aws cloudformation validate-template --template-body "file://$HERE/compute.yaml" >/dev/null
(( APPLY )) || { log "미리보기만 — 만들기: AWS_PROFILE=$AWS_PROFILE bash $0 --apply"; exit 0; }

if ! exists "$NET"; then
  aws cloudformation create-stack --stack-name "$NET" --template-body "file://$HERE/network.yaml" >/dev/null
  log "$NET 만드는 중…"
  aws cloudformation wait stack-create-complete --stack-name "$NET" \
    || die "$NET 실패 — aws cloudformation describe-stack-events --stack-name $NET"
fi
if ! exists "$STACK"; then
  aws cloudformation create-stack --stack-name "$STACK" --template-body "file://$HERE/compute.yaml" \
    --capabilities CAPABILITY_NAMED_IAM --parameters "ParameterKey=UbuntuAmiId,ParameterValue=$AMI" >/dev/null
  log "$STACK 만드는 중 (몇 분)…"
  aws cloudformation wait stack-create-complete --stack-name "$STACK" \
    || die "$STACK 실패 — aws cloudformation describe-stack-events --stack-name $STACK"
fi
log "완료 — vm1 $(out DrControlInstanceId) $(state "$(out DrControlInstanceId)") · vm2 $(out DbInstanceId) $(state "$(out DbInstanceId)")"
warn "다음: VPN 가입 키 → AWS_PROFILE=$AWS_PROFILE bash cloud/infra/put-join-key.sh → up.sh (안 쓰면 down.sh)"
