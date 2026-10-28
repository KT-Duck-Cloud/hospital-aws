#!/usr/bin/env bash
# infra/aws 공통 — Mac 에서 실행하는 스크립트가 source 한다
set -euo pipefail
export AWS_PROFILE=${AWS_PROFILE:-aws-agent}
export AWS_REGION=ap-southeast-2          # 프로젝트의 선택된 리전 — 다른 리전에는 만들지 않는다
export AWS_PAGER=""
STACK=duckcloud-compute
PARAM=/duckcloud/aws
LOGIN=https://vpn.example.org:63728
IMG_TAG=9e8e706                            # (옛 방식 · 기록) 초기이미지 tar — put-image.sh 만 씀
# 1조와 같은 앱 — KT-Duck-Cloud/hospital-gitops 의 차트 · 1조 레지스트리 이미지 (deploy/aws/values-t8.yaml)
GITOPS_REF=${GITOPS_REF:-main}             # 1조가 배포한 커밋 · 브랜치로 맞춘다
REG=${REG:-100.64.0.41:8086/duckcloud}     # 1조 레지스트리 (t4-vm1, VPN) — values-onprem.yaml 과 같게
APP_TAG=${APP_TAG:-latest}                 # 1조 values-onprem.yaml 의 tag 와 같게
SLOT=8                                     # Headscale t8@ — t8-vm1 .81 · t8-vm2 .82 (t7 = dr-ctl, ctl VMware)
HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)

log()  { printf '\033[1;34m[aws]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[aws]\033[0m %s\n' "$*"; }
die()  { printf '\033[1;31m[aws]\033[0m %s\n' "$*" >&2; exit 1; }

aws sts get-caller-identity --query Account --output text >/dev/null 2>&1 \
  || die "AWS 로그인 필요: aws login --profile $AWS_PROFILE"

out() {  # out <OutputKey> — duckcloud-compute 스택 출력값
  aws cloudformation describe-stacks --stack-name "$STACK" \
    --query "Stacks[0].Outputs[?OutputKey=='$1'].OutputValue" --output text
}

state() { aws ec2 describe-instances --instance-ids "$@" --query 'Reservations[].Instances[].State.Name' --output text; }

# 지금 AWS 조가 맡은 역할 (1조 · 2조 · 3조 · 없음) — ctl 메쉬의 이름표에서. 읽지 못하면 "?"
aws_role() {
  curl -fsS --max-time 8 "$LOGIN/join/names.json" 2>/dev/null | python3 -c '
import json, sys
d = json.load(sys.stdin)
print(" ".join(t for t, s in (d.get("sites") or {}).items() if s.get("member") == "t'"$SLOT"'") or "없음")' 2>/dev/null || echo "?"
}
