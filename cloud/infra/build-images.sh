#!/usr/bin/env bash
# 1조 레지스트리(Harbor t4-vm1 100.64.0.41:8086/duckcloud)에 앱 이미지가 없을 때 — 같은 저장소 main 을 Mac 에서 amd64 로 빌드해
# 같은 이름(100.64.0.41:8086/duckcloud/reservation-app-*:latest)으로 AWS 조 두 노드 k3s containerd 에 넣고 고정한다 (2026-10-20)
#   bash cloud/infra/build-images.sh            ../KT-Duck-Cloud/hospital-* 저장소 (origin/main 으로 맞춘 뒤)
#   이미지 이름이 1조 values.yaml 과 같아 차트 · values-t8.yaml 은 그대로. Harbor 가 다시 채워지면 up.sh 의 img_pull 이 그쪽에서 받는다
source "$(dirname "$0")/lib.sh"
SRC=${SRC:-$HERE/../../../KT-Duck-Cloud}
OUT=$(mktemp -d); trap 'rm -rf "$OUT"' EXIT
REF=100.64.0.41:8086/duckcloud/reservation-app
refs=()
for p in "auth hospital-backend-auth" "reservation hospital-backend-reservation" "stats hospital-backend-stats" "web hospital-frontend"; do
  set -- $p
  [[ -d $SRC/$2/.git ]] || die "저장소 없음: $SRC/$2"
  [[ -z $(git -C "$SRC/$2" status --porcelain) ]] || die "$2 에 커밋 안 한 변경이 있음"
  git -C "$SRC/$2" fetch -q origin && git -C "$SRC/$2" merge -q --ff-only origin/main
  log "$1 ← $2 $(git -C "$SRC/$2" log -1 --format='%h %cd' --date=short)"
  docker buildx build -q --platform linux/amd64 --load -t "$REF-$1:latest" \
    --label org.opencontainers.image.revision="$(git -C "$SRC/$2" rev-parse HEAD)" "$SRC/$2" >/dev/null
  refs+=("$REF-$1:latest")
done
docker save "${refs[@]}" -o "$OUT/app-latest.tar"
BUCKET=$(out ArtifactBucketName)
aws s3 cp --only-show-errors "$OUT/app-latest.tar" "s3://$BUCKET/images/app-latest.tar"
log "s3://$BUCKET/images/app-latest.tar ($(du -h "$OUT/app-latest.tar" | cut -f1)) → 두 노드"

j=$(mktemp)
python3 - "$BUCKET" > "$j" <<'PY'
import json, sys
b = sys.argv[1]
cmd = ("set -e; T=$(curl -fsS -X PUT http://169.254.169.254/latest/api/token -H 'X-aws-ec2-metadata-token-ttl-seconds: 60'); "
       "R=$(curl -fsS -H \"X-aws-ec2-metadata-token: $T\" http://169.254.169.254/latest/meta-data/placement/region); "
       f"aws s3 cp --region $R --only-show-errors s3://{b}/images/app-latest.tar /var/tmp/app-latest.tar; "
       "k3s ctr -n k8s.io images import /var/tmp/app-latest.tar >/dev/null; rm -f /var/tmp/app-latest.tar; "
       "for n in auth reservation stats web; do k3s ctr -n k8s.io images label 100.64.0.41:8086/duckcloud/reservation-app-$n:latest "
       "io.cri-containerd.pinned=pinned >/dev/null; done; k3s ctr -n k8s.io images ls | awk '/duckcloud/{print $1, $4, $5}'")
print(json.dumps({"commands": [cmd], "executionTimeout": ["900"]}))
PY
for id in $(out DrControlInstanceId) $(out DbInstanceId); do
  cid=$(aws ssm send-command --instance-ids "$id" --document-name AWS-RunShellScript --comment "duckcloud images" \
          --parameters "file://$j" --query Command.CommandId --output text)
  for i in $(seq 1 60); do
    st=$(aws ssm get-command-invocation --command-id "$cid" --instance-id "$id" --query Status --output text 2>/dev/null || echo Pending)
    [[ $st == Pending || $st == InProgress || $st == Delayed ]] || break; sleep 5
  done
  aws ssm get-command-invocation --command-id "$cid" --instance-id "$id" --query StandardOutputContent --output text | sed "s/^/   $id /"
  [[ $st == Success ]] || { rm -f "$j"; die "이미지 넣기 실패 ($id $st)"; }
done
rm -f "$j"
log "완료 — 이어서 bash cloud/infra/up.sh (img_pull 이 레지스트리에 닿지 않으면 이 이미지를 쓴다)"
