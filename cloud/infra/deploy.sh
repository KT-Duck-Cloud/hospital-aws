#!/usr/bin/env bash
# compute.yaml → 스택 duckcloud-compute. 기본은 변경 미리보기(change set)만 보여 주고 지운다
#   bash cloud/infra/deploy.sh            미리보기
#   bash cloud/infra/deploy.sh --apply    적용 — vm1(기존 EC2)이 교체되는 변경이면 적용하지 않고 멈춘다
# 새 vm2 는 켜진 상태로 만들어진다 → 바로 up.sh 로 설정하거나 down.sh 로 끈다
source "$(dirname "$0")/lib.sh"
APPLY=0; [[ ${1:-} == --apply ]] && APPLY=1
VM1=$(out DrControlInstanceId)
AMI=$(aws ec2 describe-instances --instance-ids "$VM1" --query 'Reservations[0].Instances[0].ImageId' --output text)
CS=duck-$(date +%Y%m%d-%H%M%S)
log "vm1 $VM1 · AMI 고정 $AMI · change set $CS"
aws cloudformation validate-template --template-body "file://$HERE/compute.yaml" >/dev/null
aws cloudformation create-change-set --stack-name "$STACK" --change-set-name "$CS" \
  --template-body "file://$HERE/compute.yaml" --capabilities CAPABILITY_NAMED_IAM \
  --parameters "ParameterKey=UbuntuAmiId,ParameterValue=$AMI" ParameterKey=InstanceType,UsePreviousValue=true >/dev/null
aws cloudformation wait change-set-create-complete --stack-name "$STACK" --change-set-name "$CS" 2>/dev/null || true
aws cloudformation describe-change-set --stack-name "$STACK" --change-set-name "$CS" \
  --query 'Changes[].ResourceChange.[Action,LogicalResourceId,ResourceType,Replacement]' --output table
# vm1 을 다시 만들 수 있는 변경이면 멈춘다. SecurityGroupIds 는 새 보안 그룹을 참조해서 '조건부'로 표시될 뿐
# VPC 인스턴스에서는 그 자리에서 바뀐다 — 그것만 허용하고, 적용 전에 vm1 디스크 스냅숏을 남긴다
RISK=$(aws cloudformation describe-change-set --stack-name "$STACK" --change-set-name "$CS" \
  --query "Changes[?ResourceChange.LogicalResourceId=='DrControlInstance'].ResourceChange.Details[?Target.RequiresRecreation!='Never'].Target.Name" --output text \
  | tr '\t' '\n' | { grep -v '^SecurityGroupIds$' || true; } | sort -u | tr '\n' ' ')
if [[ -n ${RISK// /} ]]; then
  aws cloudformation delete-change-set --stack-name "$STACK" --change-set-name "$CS"
  die "vm1 이 다시 만들어질 수 있는 변경: $RISK— 적용하지 않음. compute.yaml 을 확인하세요"
fi
if (( ! APPLY )); then
  aws cloudformation delete-change-set --stack-name "$STACK" --change-set-name "$CS"
  log "미리보기만 — 적용: bash $0 --apply"; exit 0
fi
VOL=$(aws ec2 describe-instances --instance-ids "$VM1" --query 'Reservations[0].Instances[0].BlockDeviceMappings[0].Ebs.VolumeId' --output text)
SNAP=$(aws ec2 create-snapshot --volume-id "$VOL" --description "duckcloud vm1 before $CS" \
  --tag-specifications 'ResourceType=snapshot,Tags=[{Key=Name,Value=duckcloud-vm1-before-deploy}]' --query SnapshotId --output text)
log "vm1 디스크 스냅숏 $SNAP (확인 뒤 지워도 됨: aws ec2 delete-snapshot --snapshot-id $SNAP)"
aws ec2 wait snapshot-completed --snapshot-ids "$SNAP"
aws cloudformation execute-change-set --stack-name "$STACK" --change-set-name "$CS"
log "적용 중 (몇 분)…"
aws cloudformation wait stack-update-complete --stack-name "$STACK" \
  || die "적용 실패 — aws cloudformation describe-stack-events --stack-name $STACK"
NEW1=$(out DrControlInstanceId)
[[ $NEW1 == "$VM1" ]] || warn "vm1 이 다시 만들어짐 ($VM1 → $NEW1) — 옛 디스크는 스냅숏 $SNAP"
log "완료 — vm1 $(state "$NEW1") · vm2 $(out DbInstanceId) $(state "$(out DbInstanceId)")"
warn "vm2 는 켜진 상태로 만들어졌습니다 — 이어서 up.sh, 아니면 down.sh"
