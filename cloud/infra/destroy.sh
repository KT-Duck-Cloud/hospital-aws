#!/usr/bin/env bash
# 10-29 발표 뒤 AWS 전체 삭제 (dashboard/aws_schedule.json "종료 후 전체 삭제"). 되돌릴 수 없다
#   bash cloud/infra/destroy.sh                  지울 목록만
#   bash cloud/infra/destroy.sh --apply          지움 — 'delete duckcloud-aws' 를 직접 입력해야 진행
#     --with-backups   S3 백업 버킷 · ECR 이미지까지 (storage 스택). 없으면 storage 스택은 남긴다
#     --with-snapshots RDS 마지막 스냅숏 · AMI 백업까지
source "$(dirname "$0")/lib.sh"
APPLY=0; BK=0; SNAP=0
for a in "$@"; do case $a in --apply) APPLY=1 ;; --with-backups) BK=1 ;; --with-snapshots) SNAP=1 ;; esac; done
BUCKET=duckcloud-backup-$(aws sts get-caller-identity --query Account --output text)-$AWS_REGION
STACKS=(duckcloud-compute); (( BK )) && STACKS+=(duckcloud-storage); STACKS+=(duckcloud-network duckcloud-budgets)
log "지울 것 (리전 $AWS_REGION):"
printf '   스택  %s\n' "${STACKS[@]}"
echo "   SSM   $PARAM/* (가입 키 · k3s 토큰)"
(( BK ))   && echo "   S3    $BUCKET 내용 + ECR 이미지"
(( SNAP )) && echo "   RDS 스냅숏 duckcloud-mysql-final-20261010 · AMI ec2_ami_backup 과 그 EBS 스냅숏"
(( APPLY )) || { log "목록만 — 지우려면 --apply"; exit 0; }
read -rp "되돌릴 수 없습니다. 'delete duckcloud-aws' 입력: " ok
[[ $ok == "delete duckcloud-aws" ]] || die "취소"

for n in $(aws ssm get-parameters-by-path --path "$PARAM" --query 'Parameters[].Name' --output text); do
  aws ssm delete-parameter --name "$n"; done
if (( BK )); then
  aws s3 rm "s3://$BUCKET" --recursive
  for r in $(aws ecr describe-repositories --query 'repositories[].repositoryName' --output text); do
    [[ $r == hospital/* ]] && aws ecr batch-delete-image --repository-name "$r" \
      --image-ids "$(aws ecr list-images --repository-name "$r" --query imageIds --output json)" >/dev/null 2>&1 || true
  done
fi
for s in "${STACKS[@]}"; do
  log "스택 삭제 $s"
  aws cloudformation delete-stack --stack-name "$s"
  aws cloudformation wait stack-delete-complete --stack-name "$s" || die "$s 삭제 실패 — describe-stack-events 확인"
done
if (( SNAP )); then
  aws rds delete-db-snapshot --db-snapshot-identifier duckcloud-mysql-final-20261010 >/dev/null || true
  for ami in $(aws ec2 describe-images --owners self --filters Name=name,Values=ec2_ami_backup --query 'Images[].ImageId' --output text); do
    snaps=$(aws ec2 describe-images --image-ids "$ami" --query 'Images[0].BlockDeviceMappings[].Ebs.SnapshotId' --output text)
    aws ec2 deregister-image --image-id "$ami"
    for s in $snaps; do aws ec2 delete-snapshot --snapshot-id "$s"; done
  done
fi
log "완료 — 남은 과금 리소스는 AWS 콘솔 Billing 에서 확인"
