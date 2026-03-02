# Kubernetes / OpenShift デプロイガイド

本システムを Kubernetes (または OpenShift) クラスタにデプロイし、
`kubernetes-mcp-server` が K8s API にアクセスするための認証・RBAC 設定を解説する。

## 概要

`kubernetes-mcp-server` ([containers/kubernetes-mcp-server](https://github.com/containers/kubernetes-mcp-server))
は Kubernetes API を MCP プロトコルで公開する Go 製サーバ。
`--read-only` モードで起動すると読み取り専用操作のみに制限される。

クラスタ内 Pod として稼働する場合、ServiceAccount トークンが自動マウントされ、
kubeconfig なしで K8s API に接続できる。必要な権限は RBAC で付与する。

## 認証の仕組み

### In-Cluster 認証 (推奨)

Kubernetes 上で Pod として動作する場合、以下が自動的に提供される:

| パス | 内容 |
|------|------|
| `/var/run/secrets/kubernetes.io/serviceaccount/token` | ServiceAccount トークン (JWT) |
| `/var/run/secrets/kubernetes.io/serviceaccount/ca.crt` | クラスタ CA 証明書 |
| `/var/run/secrets/kubernetes.io/serviceaccount/namespace` | Pod の Namespace |

`kubernetes-mcp-server` は Go の `client-go` ライブラリを使用しており、
`rest.InClusterConfig()` で上記トークンを自動検出する。
**kubeconfig ファイルのマウントは不要。**

### kubeconfig 認証 (開発用)

Docker Compose 環境など、クラスタ外から接続する場合は kubeconfig をマウントする:

```yaml
volumes:
  - ${KUBECONFIG:-~/.kube/config}:/home/mcp/.kube/config:ro
```

## RBAC 設定

### 読み取り専用 ClusterRole

KubernetesAgent が使用するツールに必要な最小権限:

```yaml
apiVersion: v1
kind: ServiceAccount
metadata:
  name: kubernetes-mcp
  namespace: ai-monitoring
---
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRole
metadata:
  name: kubernetes-mcp-readonly
rules:
  # Pod 一覧・詳細・ログ
  - apiGroups: [""]
    resources: ["pods", "pods/log"]
    verbs: ["get", "list", "watch"]
  # Event 一覧
  - apiGroups: [""]
    resources: ["events"]
    verbs: ["get", "list", "watch"]
  # Namespace 一覧
  - apiGroups: [""]
    resources: ["namespaces"]
    verbs: ["get", "list"]
  # Node (リソース使用状況)
  - apiGroups: [""]
    resources: ["nodes"]
    verbs: ["get", "list"]
  # metrics.k8s.io (kubectl top 相当)
  - apiGroups: ["metrics.k8s.io"]
    resources: ["pods", "nodes"]
    verbs: ["get", "list"]
  # Deployment, Service, PVC 等のワークロード
  - apiGroups: ["apps"]
    resources: ["deployments", "replicasets", "statefulsets", "daemonsets"]
    verbs: ["get", "list"]
  - apiGroups: [""]
    resources: ["services", "persistentvolumeclaims", "configmaps"]
    verbs: ["get", "list"]
  # NetworkPolicy
  - apiGroups: ["networking.k8s.io"]
    resources: ["networkpolicies"]
    verbs: ["get", "list"]
  # RBAC (参照のみ)
  - apiGroups: ["rbac.authorization.k8s.io"]
    resources: ["roles", "rolebindings", "clusterroles", "clusterrolebindings"]
    verbs: ["get", "list"]
---
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRoleBinding
metadata:
  name: kubernetes-mcp-readonly
subjects:
  - kind: ServiceAccount
    name: kubernetes-mcp
    namespace: ai-monitoring
roleRef:
  kind: ClusterRole
  name: kubernetes-mcp-readonly
  apiGroup: rbac.authorization.k8s.io
```

> **Note:** `ClusterRole` + `ClusterRoleBinding` はクラスタ全体のリソースを読み取れる。
> 特定の Namespace のみに制限したい場合は `Role` + `RoleBinding` を使用する。

### Namespace 限定の RBAC (オプション)

特定の Namespace (`monitoring`, `default` 等) のみに限定する場合:

```yaml
apiVersion: rbac.authorization.k8s.io/v1
kind: Role
metadata:
  name: kubernetes-mcp-readonly
  namespace: monitoring
rules:
  - apiGroups: ["", "apps", "metrics.k8s.io"]
    resources: ["pods", "pods/log", "events", "deployments", "replicasets",
                "services", "configmaps", "nodes"]
    verbs: ["get", "list", "watch"]
---
apiVersion: rbac.authorization.k8s.io/v1
kind: RoleBinding
metadata:
  name: kubernetes-mcp-readonly
  namespace: monitoring
subjects:
  - kind: ServiceAccount
    name: kubernetes-mcp
    namespace: ai-monitoring
roleRef:
  kind: Role
  name: kubernetes-mcp-readonly
  apiGroup: rbac.authorization.k8s.io
```

## Kubernetes デプロイメント

### マニフェスト

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: kubernetes-mcp
  namespace: ai-monitoring
  labels:
    app: kubernetes-mcp
spec:
  replicas: 1
  selector:
    matchLabels:
      app: kubernetes-mcp
  template:
    metadata:
      labels:
        app: kubernetes-mcp
    spec:
      serviceAccountName: kubernetes-mcp
      containers:
        - name: kubernetes-mcp
          image: ghcr.io/containers/kubernetes-mcp-server:latest
          args: ["--port", "8080", "--read-only", "--log-level", "3"]
          ports:
            - containerPort: 8080
              name: http
          resources:
            requests:
              cpu: 50m
              memory: 64Mi
            limits:
              cpu: 200m
              memory: 128Mi
          livenessProbe:
            httpGet:
              path: /healthz
              port: http
            initialDelaySeconds: 5
            periodSeconds: 30
          readinessProbe:
            httpGet:
              path: /healthz
              port: http
            initialDelaySeconds: 3
            periodSeconds: 10
---
apiVersion: v1
kind: Service
metadata:
  name: kubernetes-mcp
  namespace: ai-monitoring
spec:
  selector:
    app: kubernetes-mcp
  ports:
    - port: 8080
      targetPort: http
      name: http
```

### Agent アプリの環境変数

Agent Pod から Kubernetes MCP に接続するための設定:

```yaml
env:
  - name: MCP_KUBERNETES_URL
    value: "http://kubernetes-mcp.ai-monitoring.svc.cluster.local:8080"
  - name: MCP_KUBERNETES_TRANSPORT
    value: "streamable_http"
```

## OpenShift 固有の設定

OpenShift は標準 Kubernetes の RBAC に加えて、Security Context Constraints (SCC) によるセキュリティ制御がある。

### SCC 設定

`kubernetes-mcp-server` は非特権コンテナとして動作するため、デフォルトの `restricted` SCC で問題ない。
特別な SCC 設定は不要。

### OpenShift Route (外部公開が必要な場合)

通常、kubernetes-mcp は同一クラスタ内の Agent Pod からのみアクセスするため Route は不要。
デバッグ目的で外部からアクセスしたい場合:

```yaml
apiVersion: route.openshift.io/v1
kind: Route
metadata:
  name: kubernetes-mcp
  namespace: ai-monitoring
spec:
  to:
    kind: Service
    name: kubernetes-mcp
  port:
    targetPort: http
  tls:
    termination: edge
```

### OpenShift の ServiceAccount トークン

OpenShift 4.x では `TokenRequest` API によるバウンドトークン (有効期限付き) が自動発行される。
`kubernetes-mcp-server` の `client-go` がこれを透過的に処理するため、追加設定は不要。

旧方式 (長期トークン) が必要な場合のみ、明示的に Secret を作成する:

```yaml
apiVersion: v1
kind: Secret
metadata:
  name: kubernetes-mcp-token
  namespace: ai-monitoring
  annotations:
    kubernetes.io/service-account.name: kubernetes-mcp
type: kubernetes.io/service-account-token
```

> **推奨:** OpenShift 4.x+ では自動バウンドトークンを使用し、長期トークンの作成は避ける。

## Docker Compose (開発環境)

ローカル開発ではホストの kubeconfig を読み取り専用でマウントする:

```yaml
# docker-compose.yaml
kubernetes-mcp:
  build: ./deploy/kubernetes-mcp
  ports:
    - "9094:8080"
  volumes:
    - ${KUBECONFIG:-~/.kube/config}:/home/mcp/.kube/config:ro
  command: ["--port", "8080", "--read-only", "--log-level", "3"]
  networks:
    - monitoring
```

`.env` の設定:

```bash
MCP_KUBERNETES_URL=http://kubernetes-mcp:8080
MCP_KUBERNETES_TRANSPORT=streamable_http
```

## セキュリティに関する注意事項

- **常に `--read-only` で起動する。** 書き込み操作を許可しない
- **ClusterRole の権限は最小限に。** 不要な apiGroups / resources は追加しない
- **Namespace 限定を検討する。** 全 Namespace を読む必要がない場合は `Role` + `RoleBinding` を使う
- **NetworkPolicy で kubernetes-mcp への接続元を制限する:**

```yaml
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: kubernetes-mcp-allow-agent
  namespace: ai-monitoring
spec:
  podSelector:
    matchLabels:
      app: kubernetes-mcp
  policyTypes:
    - Ingress
  ingress:
    - from:
        - podSelector:
            matchLabels:
              app: ai-agent
      ports:
        - port: 8080
```

## 検証手順

デプロイ後の動作確認:

```bash
# 1. RBAC が正しいか確認 (ServiceAccount の権限をテスト)
kubectl auth can-i list pods --as=system:serviceaccount:ai-monitoring:kubernetes-mcp --all-namespaces
# → yes

kubectl auth can-i delete pods --as=system:serviceaccount:ai-monitoring:kubernetes-mcp --all-namespaces
# → no (read-only のため)

# 2. kubernetes-mcp Pod のログ確認
kubectl logs -n ai-monitoring deployment/kubernetes-mcp

# 3. Agent のヘルスチェックで kubernetes MCP が接続済みか確認
curl http://<agent-url>/api/v1/health
# → {"mcp_servers": {"prometheus": true, "loki": true, "grafana": true, "kubernetes": true}}
```
