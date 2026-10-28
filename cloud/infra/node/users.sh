# 팀원 접속 계정 (vm1 · vm2) — SSM Session Manager 가 IAM 역할 태그 SSMSessionRunAs 로 이 계정에 넣는다 (SSH 포트 없음)
#   duck-admin : sudo (비밀번호 없음 — 로그인 비밀번호 자체가 없고 Session Manager 로만 들어옴). IaC · 소유자/관리자 세션
#   duck-view  : sudo 없음. 로그 읽기(adm · systemd-journal), vm1 은 K8s 읽기 전용(ClusterRole view — Secret 제외)
#   up.sh 가 common.sh 뒤에 붙여 root 로 보낸다. 여러 번 실행해도 안전
duck_user() {  # duck_user <이름> <추가 그룹>
  local u=$1 g=$2
  id "$u" >/dev/null 2>&1 || { useradd -m -s /bin/bash -c "DuckCloud SSM $u" "$u"; say "계정 $u 만듦"; }
  passwd -l "$u" >/dev/null                       # 비밀번호 로그인 불가 — Session Manager 로만
  [[ -z $g ]] || usermod -aG "$g" "$u"
}
duck_user duck-admin "adm,systemd-journal"
duck_user duck-view  "adm,systemd-journal"
printf 'duck-admin ALL=(ALL) NOPASSWD:ALL\n' > /etc/sudoers.d/90-duck-admin
chmod 440 /etc/sudoers.d/90-duck-admin && visudo -cf /etc/sudoers.d/90-duck-admin >/dev/null || die "sudoers 문법 오류"
rm -f /etc/sudoers.d/90-duck-view                 # view 는 sudo 를 주지 않는다
id -nG duck-view | grep -qw sudo && gpasswd -d duck-view sudo >/dev/null || true

# vm1(k3s 서버)만 — duck-view 에 K8s 읽기 전용 kubeconfig, duck-admin 에 관리자 kubeconfig
if systemctl is-active --quiet k3s 2>/dev/null; then
  KC="k3s kubectl"
  $KC apply -f - >/dev/null <<'YAML'
apiVersion: v1
kind: ServiceAccount
metadata: {name: duck-view, namespace: kube-system}
---
apiVersion: v1
kind: Secret
metadata:
  name: duck-view-token
  namespace: kube-system
  annotations: {kubernetes.io/service-account.name: duck-view}
type: kubernetes.io/service-account-token
---
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRoleBinding
metadata: {name: duck-view}
roleRef: {apiGroup: rbac.authorization.k8s.io, kind: ClusterRole, name: view}
subjects: [{kind: ServiceAccount, name: duck-view, namespace: kube-system}]
YAML
  for i in $(seq 1 10); do TOK=$($KC -n kube-system get secret duck-view-token -o jsonpath='{.data.token}' 2>/dev/null | base64 -d); [[ -n $TOK ]] && break; sleep 2; done
  [[ -n ${TOK:-} ]] || die "duck-view 토큰이 만들어지지 않음"
  CA=$(base64 -w0 /var/lib/rancher/k3s/server/tls/server-ca.crt)
  install -d -m 700 -o duck-view -g duck-view /home/duck-view/.kube
  cat > /home/duck-view/.kube/config <<EOF
apiVersion: v1
kind: Config
clusters: [{name: duck-aws, cluster: {server: "https://127.0.0.1:6443", certificate-authority-data: "$CA"}}]
users: [{name: duck-view, user: {token: "$TOK"}}]
contexts: [{name: duck-view, context: {cluster: duck-aws, user: duck-view}}]
current-context: duck-view
EOF
  unset TOK
  chown duck-view:duck-view /home/duck-view/.kube/config; chmod 600 /home/duck-view/.kube/config
  install -d -m 700 -o duck-admin -g duck-admin /home/duck-admin/.kube
  install -m 600 -o duck-admin -g duck-admin /etc/rancher/k3s/k3s.yaml /home/duck-admin/.kube/config
  grep -q 'k3s kubectl' /etc/profile.d/duck-kubectl.sh 2>/dev/null || \
    printf 'command -v kubectl >/dev/null || alias kubectl="k3s kubectl --kubeconfig=$HOME/.kube/config"\nexport KUBECONFIG=$HOME/.kube/config\n' > /etc/profile.d/duck-kubectl.sh
  say "K8s 접속 — duck-view: 읽기 전용(view, Secret 제외) · duck-admin: 관리자"
fi
say "접속 계정 — duck-admin(sudo) · duck-view(읽기) $(id -nG duck-view | tr ' ' ',')"
