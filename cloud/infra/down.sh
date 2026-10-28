#!/usr/bin/env bash
# AWS 조 끄기 — 추가 워커 0대 · vm1 · vm2 정지. 꺼져 있는 동안 과금은 EBS(약 $2.5/월)뿐
#   bash cloud/infra/down.sh
#   bash cloud/infra/down.sh --force     AWS 조가 지금 1조(운영)이거나 역할을 확인하지 못해도 끔
source "$(dirname "$0")/lib.sh"
FORCE=0; [[ ${1:-} == --force ]] && FORCE=1
VM1=$(out DrControlInstanceId); VM2=$(out DbInstanceId); ASG=$(out DrAsgName)
ROLE=$(aws_role)
case $ROLE in
  *1조*) (( FORCE )) || die "AWS 조가 지금 1조(운영)입니다 — 끄면 서비스가 멈춥니다. 정말이면 --force" ;;
  "?")   (( FORCE )) || die "ctl 에서 역할을 읽지 못함 (ctl 장애면 AWS 가 운영 중일 수 있음). 확인 후 --force" ;;
  없음)  ;;
  *)     warn "AWS 조가 지금 $ROLE 입니다 — 끄면 5분 뒤 메쉬가 역할을 다른 조로 넘깁니다" ;;
esac
aws autoscaling set-desired-capacity --auto-scaling-group-name "$ASG" --desired-capacity 0
IDS=("$VM1"); [[ -n $VM2 && $VM2 != None ]] && IDS+=("$VM2")
aws ec2 stop-instances --instance-ids "${IDS[@]}" >/dev/null
aws ec2 wait instance-stopped --instance-ids "${IDS[@]}"
W=$(aws autoscaling describe-auto-scaling-groups --auto-scaling-group-names "$ASG" --query 'length(AutoScalingGroups[0].Instances)' --output text)
log "꺼짐 — ${IDS[*]} $(state "${IDS[@]}" | tr '\t' ' ') · 남은 워커 $W 대(종료 중이면 곧 0)"
