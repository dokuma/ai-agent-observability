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

## Qdrant (ベクトル検索)

セマンティック検索を有効にするには Qdrant ベクトル DB と Embedding API が必要。
デフォルトでは `qdrant.enabled: false` のため、有効化しない限り既存の BM25 検索のみで動作する。

### Helm Chart でのデプロイ

Qdrant は[公式 Helm Chart](https://github.com/qdrant/qdrant-helm)を依存チャートとして利用する。
`Chart.yaml` に定義済みのため、`values.yaml` で有効化するだけでデプロイできる。

`values.yaml` で Qdrant を有効化する:

```yaml
qdrant:
  enabled: true
  replicaCount: 1
  persistence:
    size: 5Gi
  resources:
    requests:
      cpu: 100m
      memory: 256Mi
    limits:
      cpu: 500m
      memory: 1Gi

config:
  qdrantEnabled: "true"
  embeddingModel: "text-embedding-3-small"
  embeddingDimensions: "0"  # 0 = モデルデフォルト
```

`qdrant:` セクションの設定は公式チャートの values をそのまま上書きできる。
利用可能なパラメータは [qdrant-helm values.yaml](https://github.com/qdrant/qdrant-helm/blob/main/charts/qdrant/values.yaml) を参照。

Embedding API キーが必要な場合は `secrets` セクションに設定する:

```yaml
secrets:
  embeddingApiKey: "sk-..."
```

Embedding エンドポイントは LLM と異なる場合のみ指定する。
未指定時は `externalServices.llm.endpoint` がフォールバックとして使用される:

```yaml
# embedding_endpoint は ConfigMap ではなく extraEnv で設定
agent:
  extraEnv:
    - name: EMBEDDING_ENDPOINT
      value: "https://api.openai.com/v1"
```

### デプロイ手順

```bash
# 1. values.yaml を編集（上記参照）
# 2. 依存チャートの更新（qdrant チャートのダウンロード）
helm dependency update deploy/helm/ai-agent-monitoring/

# 3. デプロイ（新規 or アップグレード）
helm upgrade --install ai-agent-monitoring deploy/helm/ai-agent-monitoring/ \
  -n ai-monitoring \
  --set qdrant.enabled=true \
  --set config.qdrantEnabled=true \
  --set secrets.embeddingApiKey="sk-..."

# 4. Qdrant Pod の起動確認
kubectl get pods -n ai-monitoring -l app.kubernetes.io/name=qdrant

# 5. Qdrant ヘルスチェック
kubectl exec -n ai-monitoring deploy/ai-agent-monitoring-agent -- \
  wget -qO- http://ai-agent-monitoring-qdrant.ai-monitoring.svc:6333/healthz
```

### アーキテクチャ

公式 Helm Chart により以下の Kubernetes リソースが作成される:

| リソース | 名前 | 説明 |
|---------|------|------|
| StatefulSet | `<release>-qdrant` | Qdrant サーバー |
| Service | `<release>-qdrant` | ClusterIP (6333: HTTP, 6334: gRPC, 6335: P2P) |
| PVC | `qdrant-storage-<release>-qdrant-*` | StatefulSet の volumeClaimTemplate |

Agent Pod は起動時に自動的に:
1. Qdrant コレクション（`rca_reports`）を作成
2. SQLite に保存済みのレポートを Qdrant にマイグレーション（差分のみ）
3. 以後の検索は BM25 + Vector の RRF ハイブリッドで実行

### 縮退運転

Qdrant が停止・未接続の場合でも Agent は正常に動作する:

| 状況 | 動作 |
|------|------|
| `qdrant.enabled: false` | BM25 のみで検索（既存動作） |
| Qdrant Pod が停止中 | ベクトル検索をスキップし BM25 のみで検索 |
| Embedding API が不通 | レポートの SQLite 保存は成功、ベクトル登録のみスキップ |

### 永続化とバックアップ

Qdrant のデータは StatefulSet の PVC に永続化される。
ベクトルデータは SQLite のレポートから再構築可能なため、
PVC を失った場合は Agent の再起動で自動マイグレーションが実行される。

バックアップの優先順位:
1. **SQLite DB** (`/app/data/rca_reports.db`) — 正本。必ずバックアップする
2. **Qdrant PVC** — オプション。失っても SQLite から再構築可能

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
MCP_KUBERNETES_TRANSPORT=sse
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

# 4. MCP プロトコルレベルの診断（セッション初期化のテスト）
curl http://<agent-url>/api/v1/health/mcp-diagnose
# → {"kubernetes": {"transport": "sse", "status": "ok", "details": "Connected, 8 tools available"}}
```

## トラブルシューティング

### MCP トランスポートの互換性

`kubernetes-mcp-server` は Go SDK (`modelcontextprotocol/go-sdk`) を使用しており、
Python MCP SDK との間でプロトコルの互換性に制約がある。

**推奨トランスポート: SSE**

```bash
MCP_KUBERNETES_TRANSPORT=sse
```

Streamable HTTP (`/mcp`) は Go SDK のミドルウェア（RequestMiddleware, AuthorizationMiddleware）が
ResponseWriter をラッピングするため、プロトコルレベルでの接続失敗が発生する場合がある。

### 接続エラーの診断

| エラー | 原因 | 対策 |
|---|---|---|
| `McpError: Connection closed` | SSE ストリームが初期化中に切断された | サーバーログを確認、kubeconfig の有効性を確認 |
| `RemoteProtocolError: Server disconnected` | Streamable HTTP ハンドラーがリクエストを処理できなかった | `MCP_KUBERNETES_TRANSPORT=sse` に変更 |
| `RemoteProtocolError: peer closed connection without sending complete message body (incomplete chunked read)` | SSE レスポンスが大きすぎて chunked transfer が途中で切断された | 下記「SSE レスポンスサイズと接続安定性」を参照 |
| `MCPConnectionError: connection refused` | サーバーが起動していない | `kubectl logs` / `docker logs` で起動ログを確認 |
| `/healthz` は OK だが MCP 接続失敗 | HTTP は正常だがプロトコルハンドシェイク失敗 | `mcp-diagnose` エンドポイントで詳細診断 |

### SSE レスポンスサイズと接続安定性

#### 背景

SSE (Server-Sent Events) トランスポートは HTTP の chunked transfer encoding を使用する
長寿命接続であり、プロトコルレベルでのレスポンスサイズ制限は定義されていない。
しかし実運用において、**レスポンスが大きいツール呼び出しで SSE ストリームが途中で切断される**
事象が確認された。

#### 発見の経緯

kubernetes-mcp-server (Go SDK) に対して複数のツールを呼び出した際、
以下のパターンが観測された:

| ツール | 結果 | レスポンスサイズ |
|---|---|---|
| `events_list` | 成功 | 小〜中（イベント一覧） |
| `pods_top` | 成功 | 小（メトリクスデータ） |
| `pods_list`（全 namespace） | **失敗** | **大**（クラスタ全 Pod の完全な仕様） |

失敗時のエラー:
```
httpcore.RemoteProtocolError: peer closed connection without sending complete message body (incomplete chunked read)
```

同一 SSE セッション内で小さなレスポンスは正常に返却され、
大きなレスポンスでのみ切断が発生したことから、
**レスポンスサイズが SSE 接続の安定性に直接影響する**ことが判明した。

#### 原因の詳細

SSE トランスポートでは、MCP ツールの結果は1つの SSE イベントとして送信される。
Go MCP SDK の `writeEvent()` は結果の JSON 全体を単一の `fmt.Fprintf` + `Flush()` で
書き出すため、大きなレスポンスでは以下の問題が発生しうる:

1. **Go HTTP サーバーのバッファ**: `net/http` は 4KB の書き込みバッファを持ち、
   大きなデータは複数回のバッファフラッシュが必要になる。
   `WriteTimeout` 設定時はフラッシュ中にタイムアウトする可能性がある。

2. **ミドルウェアの ResponseWriter ラッピング**: kubernetes-mcp-server の
   `RequestMiddleware` は `loggingResponseWriter` で ResponseWriter をラップする。
   `Flush()` は委譲されるが、大きな単一 Write の処理中に
   ミドルウェアのライフサイクル管理と競合する可能性がある。

3. **Kubernetes API の応答遅延**: 全 namespace の Pod 一覧など大量データの取得は
   K8s API Server 自体の応答に時間がかかり、
   その間に SSE クライアント側のタイムアウトに達する場合がある。

明確な「何 KB まで」というハードリミットは存在しないが、
レスポンスが大きいほど転送時間が長くなり、接続が途切れるリスクが高まる。

#### 対策

本システムでは以下の対策を実装している:

1. **全 namespace 一括取得の禁止**: `pods_list`（namespace パラメータなし）を
   使用せず、`pods_list_in_namespace` のみを使用。
   `k8s_list_pods` ツールの `namespace` パラメータを必須化。

2. **SSE セッション再利用**: 各ツール呼び出しで新規 SSE 接続を作成する代わりに、
   `KubernetesMCPTool.session_context()` で単一セッションを再利用。
   接続チャーン（急速な接続/切断の繰り返し）による Go サーバーの不安定化を防止。

3. **トランスポート自動検出**: 起動時に SSE / Streamable HTTP の両方を試行し、
   動作するトランスポートを自動選択。SSE が失敗する場合は
   Streamable HTTP へフォールバック。

#### 一般的なガイドライン

MCP ツール設計において、SSE トランスポート使用時は以下に注意する:

- **レスポンスを小さく保つ**: フィルタパラメータ（namespace、labelSelector 等）で
  クエリ範囲を限定し、不必要に大きなレスポンスを避ける
- **全件取得を避ける**: `*_list`（全件）より `*_list_in_*`（範囲指定）を優先する
- **セッションを再利用する**: 複数ツール呼び出しで毎回接続を作り直さない
- **Streamable HTTP の検討**: SSE で大きなレスポンスが問題になる場合、
  Streamable HTTP (`/mcp`) はリクエスト/レスポンス型のため
  長寿命接続の問題を回避できる可能性がある
  （ただし Go SDK のミドルウェア互換性を事前に検証すること）

### デバッグログの有効化

```bash
# .env に追加
LOG_LEVEL=DEBUG
```

`LOG_LEVEL=DEBUG` を設定すると、MCP 接続の詳細ログが出力される:
- SSE セッション作成時の session_id
- MCP initialize リクエスト送信
- サーバー情報（name, version）
- エラー発生時の詳細なスタックトレース
