# AWS 조 — 1조가 멈추면 승격돼 운영을 이어받는 대기 조 (평소 꺼 둠, 과금 OFF)

> **참고용 — 이 저장소에서는 실행하지 않습니다.** 대시보드 데모는 실제 계정 대신 `cloud/demo.py` 가짜 리전을 씁니다. 아래는 실제 운영에서 쓰던 구성 기록입니다.

> 리전 ap-southeast-2(시드니, 프로젝트의 선택된 리전) · Headscale 슬롯 8(`t8@`, 7 은 dr-ctl) · 메쉬 역할 `AWS` (hsagent.json `aws_slot`)
> 관련: `ops/hsagent/mesh.py` · `provision.py` (app_prepare · app_up · app_down) · `deploy/aws/values-t8.yaml` · `infra/ctl/20_aws_site.sh`
> 앱 = **1조와 같은 구성** — GitHub [KT-Duck-Cloud/hospital-gitops](https://github.com/KT-Duck-Cloud/hospital-gitops) 차트 · 1조 레지스트리 이미지 (2026-10-16 부터. 옛 초기이미지 9e8e706 · `deploy/k8s/site-standby-app` 은 쓰지 않음)

## 1조 대응표 — 2조 AWS 가 1조를 그대로 대신하도록

| 항목 | 1조 (t3, hospital-gitops) | 2조 AWS (t8) | 맞추는 곳 |
| :--- | :--- | :--- | :--- |
| 입구 | Traefik `infra/traefik/traefik.yaml` NodePort **30080** (대시보드 30088) | 같은 파일 (k3s 내장 Traefik 은 끔) | `node/app.sh` 2 |
| 앱 | `helm rsv` · ns `hospital` · `values.yaml` (10-18 하나로 합쳐짐) | `helm rsv` · ns `hospital` · `values.yaml` + `deploy/aws/values-t8.yaml` (읽기 전용 · 0개) | `node/app.sh` 5 |
| 서비스 | web · auth · reservation · stats, NodePort **30800 · 30801 · 30802** | 차트 그대로 (같은 포트) · 평소 0개 | 차트 |
| 이미지 | `100.64.0.41:8086/duckcloud/reservation-app-*:latest` | 같은 레지스트리 · 태그를 대기 중에 받아 **고정(pinned)** — DR 때 레지스트리가 없어도 뜸 | `common.sh img_pull` (vm1 · vm2) |
| DB | t3-vm2 MySQL `hospital` (쓰기) · `db-service` | t8-vm2 MySQL 8.4 복제본 (GTID, `REPLICATE_DO_DB=hospital`) · `db-service` → t8-vm2 VPC 주소 | 역할 준비 (`mysql_replica.sh`) · `app.sh` 4 |
| DB 계정 | `app_runtime` · `app_readonly` (REQUIRE SSL) | 같은 이름 · 권한 · REQUIRE SSL — 복제본에 따로 만듦 (복제 필터 때문에 1조 계정은 넘어오지 않음), 비밀번호는 AWS 것 | `provision._helm_prepare` |
| Secret | db-runtime · db-readonly · **jwt · medical-key** | db 2종은 AWS 값, **jwt · medical-key 는 1조 t3-vm1 값을 그대로 복사** (진료기록이 1조 키로 암호화돼 복제됨) | `provision._copy_site1_secrets` |
| 쓰기 | `readOnlyMode: false` | `true` → 승격 때 `--set readOnlyMode=false` · 서비스마다 1개 | `provision._helm_up` |
| 판정 | `/readyz` (DB SELECT 1) | **30800**/readyz (30080/readyz 는 web 화면이 200 으로 답해 쓰지 않음) | `mesh.app_ready` · `_helm_up` |
| 공개 입구 | frontdoor upstream `100.64.0.31 · .32:30080` | 승격 때 `100.64.0.81 · .82:30080` | `mesh.set_frontdoor` (그대로) |
| 감시 | node-exporter 9100 · mysqld_exporter 9104 (vm2) | 같음 — node-exporter 는 VPN 가입 때, mysqld_exporter 는 2조 AWS 담당용 가이드 | 모니터링 사이트 'exporter 설치' |

## 구성

| 역할 | AWS | VPN | 하는 일 |
| :--- | :--- | :--- | :--- |
| vm1 | EC2 `duckcloud-dr-control` (t3.small, 30GB — 이름만 옛것) | `t8-vm1` 100.64.0.81 | k3s 서버(마스터) · Traefik 30080 · helm rsv (평소 0개) |
| vm2 | EC2 `duckcloud-aws-vm2` (t3.small, 20GB) | `t8-vm2` 100.64.0.82 | MySQL 8.4 복제본 + k3s 에이전트(워커) |
| 추가 워커 | ASG `duckcloud-dr-app-asg` 0~2대 | 없음 (VPC 안에서 vm1 에 합류) | 부족할 때만 |
| 이미지 | 1조 레지스트리 `100.64.0.41:8086` (VPN) | — | `up.sh` 때 vm1 · vm2 가 받아 고정. (옛 S3 tar · put-image.sh 는 기록용) |

- **VPN:** 팀원 VM 과 같은 `https://vpn.example.org:63728/join/install.sh` + 가입 키(SSM 에 잠깐, 가입 뒤 지움)로 같은 Headscale 메쉬에 들어갑니다. 추가 비용 없음.
- **바깥에서 들어오는 포트**는 tailscale 직결 41641/udp 하나. SSH 는 열지 않고 노드 작업은 SSM Run Command. 공인 IP 는 켤 때 자동(EIP 없음).
- **비밀값**은 SSM Parameter Store `/duckcloud/aws/*` (`join-key` 가입 뒤 지움 · `k3s-token` · `k3s-url`). 앱 DB 비밀번호는 DR컨트롤(ctl `/etc/duckcloud/mesh/secrets.json`)과 k3s Secret 에만.

## DR 흐름 (판정: ctl duck-hsagent · 또는 dr-ctl)

| 단계 | 하는 일 |
| :--- | :--- |
| 대기 | AWS 가 켜져 10분 정상 → 메쉬가 `AWS` 역할로 편성. 관리자가 콘솔 메쉬 카드에서 **AWS 역할 준비** 승인 → 1조 DB → vm2 실시간 복제 + 앱 계정 `app_runtime` · `app_readonly`@`10.200.%` + Secret 4종 (jwt · medical-key 는 1조 복사). 앱은 0개 · `READ_ONLY_MODE=true` |
| 장애 판단 | 1조 DB 5분 무응답. 승격 순서 **2조 → 예비(3조) → AWS** — 앞 조가 안 되면(담당 없음 · 비정상 · 복제 이상) 다음 |
| 증인 | 2조 · AWS DB 어느 하나라도 1조에서 아직 받고 있으면(IO Yes) 판정 쪽 경로 문제로 보고 보류 (두 1조 방지) |
| 승격 | 1조 앱 계정 잠금 시도 → AWS vm2 받아 둔 기록 적용 · 쓰기 가능 → `helm rsv --reuse-values --set readOnlyMode=false` (쓰기 허용 · 서비스마다 1개) → `vm1:30800/readyz` 200 · `30080/healthz` web |
| 접속 전환 | `ops` · `db.duck.internal` 이름과 공개 입구(`frontdoor_auto`) upstream 이 `100.64.0.81/.82:30080` 으로 |
| 앱 실패 시 | DB 는 이미 1조 — 10분마다 앱 기동을 다시 시도 |
| 복귀 | 1조 DB 3분 응답 → 1조를 AWS 데이터로 맞춤 → AWS 쓰기 멈춤 → 1조 다시 운영 → `helm rsv --reuse-values -f values-t8.yaml --set readOnlyMode=true` (0개 · 읽기 전용 · dbPromote 끔) → AWS 다시 대기(다시 채움) |

## 쓰는 법 (Mac)

```bash
bash cloud/infra/up.sh                 # 켜기 + 설정 (vm1 → vm2 → 앱 배치, 몇 분)
bash cloud/infra/up.sh --workers 1     # + 추가 워커
bash cloud/infra/up.sh --no-setup      # 켜기만
bash cloud/infra/down.sh               # 끄기 (AWS 조가 1조이면 거부, --force)
```

`AWS_PROFILE` 기본값은 `aws-agent`. 로그인이 만료되면 `aws login --profile aws-agent`.

## 처음 한 번

1. **ctl:** Mac 에서 올린 뒤 `sudo bash ~/duckcloud-infra/20_aws_site.sh --apply`
   - `rsync -a --exclude __pycache__ ops/ user@10.10.10.8:~/duckcloud-ops/`
   - `scp infra/ctl/{20_aws_site.sh,10_update_ops.sh,headscale-acl.hujson,lib.sh} user@10.10.10.8:~/duckcloud-infra/`
   - Headscale 사용자 t8 · ACL `t8@` · hsagent.json (`slots`+8 · `aws_slot` · `frontdoor_auto`) · hsagent 새 코드
2. **스택:** `bash cloud/infra/deploy.sh` (미리보기) → `bash cloud/infra/deploy.sh --apply` — vm2 · 보안 그룹 · S3 이미지 저장소 · 권한. vm1 이 다시 만들어질 변경이면 멈춤
3. **이미지:** 따로 할 일 없음 — `up.sh` 때 vm1 · vm2 가 1조 레지스트리에서 받아 고정 (`GITOPS_REF` · `APP_TAG` 로 1조 배포와 맞춤: `GITOPS_REF=<커밋> APP_TAG=<태그> bash cloud/infra/up.sh`)
4. **가입 키:** ctl `sudo headscale users list` 로 t8 ID → `sudo headscale preauthkeys create --user <ID> --reusable --expiration 2h` → Mac `bash cloud/infra/put-join-key.sh`
5. `bash cloud/infra/up.sh` → 10분 뒤 콘솔 메쉬 카드에 AWS 역할 → **AWS 역할 준비** 승인
6. 작업이 끝나면 `bash cloud/infra/down.sh` (꺼지면 AWS 역할이 5분 뒤 공석 — 승격 후보에서 빠짐)

## 요금 (시드니 온디맨드, 추정)

| 상태 | 요금 |
| :--- | :--- |
| 꺼짐 | EBS 50GB 약 $4/월 + 스냅숏 (RDS 마지막 스냅숏 20GB · AMI 백업 8GB 약 $2.3/월) |
| 켜짐 | vm1 + vm2 시간당 약 $0.063 (t3.small 2대 + 공인 IPv4 2개). 워커는 1대당 +$0.031 |

10-29 발표 뒤 전체 삭제:
- `bash cloud/infra/destroy.sh` 로 목록을 확인합니다.
- `bash cloud/infra/destroy.sh --apply [--with-backups] [--with-snapshots]` 로 지웁니다.

`duckcloud-network` VPC 에는 이 프로젝트 밖의 보안 그룹(`jd-test-123_alb-sg`)이 있습니다. 그 그룹이 남아 있으면 네트워크 스택 삭제가 실패하므로, 먼저 확인합니다.

## 정리한 것 (2026-10-10)

- RDS `duckcloud-mysql`(MySQL 8.0 — 8.4 복제 사슬을 받을 수 없음)를 삭제했습니다. 마지막 스냅숏 `duckcloud-mysql-final-20261010` 을 남겼습니다.
- EIP 2개를 해제했습니다.
- 위임되지 않은 Route53 영역 4개(`duckcloud-dns` 스택)를 삭제했습니다.
- 해제한 IP 를 가리키던 Cloudflare A 레코드 2개를 삭제했습니다.
- 대시보드의 'Cloudflare 동기화'를 막았습니다. 이 기능은 공개 사이트 레코드를 EC2 IP 로 덮어씁니다.
- 0.1 보안 그룹 `DrControlSecurityGroup` 에는 전체 공개 22 · 80 · 443 · 1433 · 3306 · 5432 · 8080 이 남아 있습니다. vm1 이 새 그룹으로 옮겨진 뒤에는 아무 인스턴스에도 붙지 않습니다(destroy 때 함께 삭제).

## 팀 권한 (역할 전환)

팀원은 자기 AWS 계정에서 이 계정의 `duckteam/duck-<담당>-role` 로 전환합니다. IAM 사용자·액세스 키는 만들지 않습니다.
팀원 IAM 역할 (`iam/team-roles*.sh`) · 다른 조용 kubeconfig 스크립트는 권한 · 다른 담당 영역이라 이 저장소에 넣지 않았습니다.
