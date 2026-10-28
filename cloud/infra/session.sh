#!/usr/bin/env bash
# SSM Session Manager 기본 설정 (문서 SSM-SessionManagerRunShell) — 팀원 서버 접속 (SSH 포트 없이)
#   bash cloud/infra/session.sh            지금 설정 보기
#   bash cloud/infra/session.sh --apply    만들기 · 갱신
# Run As: IAM 역할 태그 SSMSessionRunAs 의 리눅스 계정으로 들어간다 (팀원 IAM 역할 — 이 저장소 밖)
#   태그 없는 세션(소유자 · 팀원 관리자 세션 AccountFullAccessRole) = duck-admin
# 계정은 node/users.sh (up.sh) 가 vm1 · vm2 에 만든다
source "$(dirname "$0")/lib.sh"
DOC=SSM-SessionManagerRunShell
APPLY=0; [[ ${1:-} == --apply ]] && APPLY=1
CONTENT=$(cat <<'JSON'
{
  "schemaVersion": "1.0",
  "description": "DuckCloud Session Manager - Run As by IAM tag SSMSessionRunAs (duck-admin / duck-view), no SSH port",
  "sessionType": "Standard_Stream",
  "inputs": {
    "runAsEnabled": true,
    "runAsDefaultUser": "duck-admin",
    "idleSessionTimeout": "20",
    "maxSessionDuration": "120",
    "shellProfile": {"linux": "cd ~ && exec bash -l", "windows": ""},
    "s3BucketName": "", "cloudWatchLogGroupName": "", "kmsKeyId": ""
  }
}
JSON
)
if aws ssm describe-document --name "$DOC" >/dev/null 2>&1; then
  log "$DOC 있음 — 지금 값:"
  aws ssm get-document --name "$DOC" --query Content --output text | python3 -c 'import json,sys;print(json.dumps(json.load(sys.stdin)["inputs"],indent=1))'
  (( APPLY )) || { log "갱신: bash $0 --apply"; exit 0; }
  v=$(aws ssm update-document --name "$DOC" --content "$CONTENT" --document-version '$LATEST' --query DocumentDescription.DocumentVersion --output text 2>&1) \
    || { [[ $v == *DuplicateDocumentContent* ]] && { log "이미 같은 내용"; exit 0; }; die "$v"; }
  aws ssm update-document-default-version --name "$DOC" --document-version "$v" >/dev/null
  log "갱신함 (버전 $v)"
else
  log "$DOC 없음 — 만들 예정 (Run As 켬 · 기본 duck-admin · 유휴 20분 · 최대 2시간)"
  (( APPLY )) || { log "만들기: bash $0 --apply"; exit 0; }
  aws ssm create-document --name "$DOC" --document-type Session --document-format JSON --content "$CONTENT" >/dev/null
  log "만듦"
fi
