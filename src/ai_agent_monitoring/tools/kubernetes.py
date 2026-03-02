"""Kubernetes MCP Tool — K8sクラスタリソース取得."""

import logging
from typing import Any

from langchain_core.tools import BaseTool, tool

from ai_agent_monitoring.tools.base import BaseMCPTool, MCPClient

logger = logging.getLogger(__name__)

# よく使う kind → apiVersion のマッピング
_KIND_API_VERSION: dict[str, str] = {
    "Pod": "v1",
    "Service": "v1",
    "ConfigMap": "v1",
    "Secret": "v1",
    "PersistentVolumeClaim": "v1",
    "Namespace": "v1",
    "Node": "v1",
    "Event": "v1",
    "ServiceAccount": "v1",
    "Deployment": "apps/v1",
    "ReplicaSet": "apps/v1",
    "StatefulSet": "apps/v1",
    "DaemonSet": "apps/v1",
    "Ingress": "networking.k8s.io/v1",
    "NetworkPolicy": "networking.k8s.io/v1",
    "Role": "rbac.authorization.k8s.io/v1",
    "RoleBinding": "rbac.authorization.k8s.io/v1",
    "ClusterRole": "rbac.authorization.k8s.io/v1",
    "ClusterRoleBinding": "rbac.authorization.k8s.io/v1",
    "CronJob": "batch/v1",
    "Job": "batch/v1",
    "HorizontalPodAutoscaler": "autoscaling/v2",
}


def _resolve_api_version(kind: str, api_version: str = "") -> str:
    """kind から apiVersion を推定（明示指定があればそちらを優先）."""
    if api_version:
        return api_version
    resolved = _KIND_API_VERSION.get(kind, "")
    if not resolved:
        logger.warning("Unknown apiVersion for kind '%s', defaulting to 'v1'", kind)
        resolved = "v1"
    return resolved


class KubernetesMCPTool(BaseMCPTool):
    """Kubernetes MCP Server 経由のクラスタリソース取得ツール群.

    containers/kubernetes-mcp-server の read-only モードで動作し、
    Pod/Event/Node/汎用リソースの情報を取得する。
    """

    async def list_pods(self, namespace: str = "") -> dict[str, Any]:
        """Pod一覧を取得.

        kubernetes-mcp-server は pods_list（全namespace）と
        pods_list_in_namespace（namespace指定）の2つのツールを持つ。
        namespace 指定時は pods_list_in_namespace を使用し、
        レスポンスサイズを抑制する。
        """
        if namespace:
            logger.info("K8s list pods in namespace: %s", namespace)
            return await self._call_tool("pods_list_in_namespace", {"namespace": namespace})
        logger.info("K8s list pods: all namespaces")
        return await self._call_tool("pods_list", {})

    async def get_pod(self, name: str, namespace: str = "default") -> dict[str, Any]:
        """Pod詳細を取得."""
        params: dict[str, Any] = {"name": name, "namespace": namespace}
        logger.info("K8s get pod: %s/%s", namespace, name)
        return await self._call_tool("pods_get", params)

    async def get_pod_logs(
        self,
        name: str,
        namespace: str = "default",
        container: str = "",
        tail: int = 100,
    ) -> dict[str, Any]:
        """Podログを取得."""
        params: dict[str, Any] = {
            "name": name,
            "namespace": namespace,
            "tail": tail,
        }
        if container:
            params["container"] = container
        logger.info("K8s get pod logs: %s/%s (tail=%d)", namespace, name, tail)
        return await self._call_tool("pods_log", params)

    async def list_events(self, namespace: str = "") -> dict[str, Any]:
        """Kubernetesイベント一覧を取得."""
        params: dict[str, Any] = {}
        if namespace:
            params["namespace"] = namespace
        logger.info("K8s list events: namespace=%s", namespace or "(all)")
        return await self._call_tool("events_list", params)

    async def list_namespaces(self) -> dict[str, Any]:
        """Namespace一覧を取得."""
        logger.info("K8s list namespaces")
        return await self._call_tool("namespaces_list", {})

    async def get_nodes_top(self) -> dict[str, Any]:
        """Nodeリソース使用状況を取得."""
        logger.info("K8s get nodes top")
        return await self._call_tool("nodes_top", {})

    async def get_pods_top(self, namespace: str = "") -> dict[str, Any]:
        """Podリソース使用状況を取得."""
        params: dict[str, Any] = {}
        if namespace:
            params["namespace"] = namespace
        logger.info("K8s get pods top: namespace=%s", namespace or "(all)")
        return await self._call_tool("pods_top", params)

    async def get_resource(
        self,
        kind: str,
        name: str = "",
        namespace: str = "",
        api_version: str = "",
    ) -> dict[str, Any]:
        """汎用リソース取得（Deployment, Service, PVC, NetworkPolicy, RBAC等）.

        kubernetes-mcp-server の resources_list / resources_get は
        apiVersion を必須パラメータとして要求する。
        """
        resolved_api_version = _resolve_api_version(kind, api_version)
        params: dict[str, Any] = {"kind": kind, "apiVersion": resolved_api_version}
        if name:
            params["name"] = name
        if namespace:
            params["namespace"] = namespace

        if name:
            logger.info("K8s get resource: %s/%s %s/%s", resolved_api_version, kind, namespace or "(cluster)", name)
            return await self._call_tool("resources_get", params)
        else:
            logger.info("K8s list resources: %s/%s namespace=%s", resolved_api_version, kind, namespace or "(all)")
            return await self._call_tool("resources_list", params)


def create_kubernetes_tools(
    mcp_client: MCPClient,
    *,
    k8s_tool: KubernetesMCPTool | None = None,
) -> list[BaseTool]:
    """LangChain Tool としてラップされた Kubernetes ツール群を生成.

    Args:
        mcp_client: MCPクライアント（k8s_tool未指定時の生成用）
        k8s_tool: 既存の KubernetesMCPTool インスタンス（セッション再利用時に指定）
    """
    k8s = k8s_tool or KubernetesMCPTool(mcp_client)

    @tool
    async def k8s_list_pods(namespace: str = "") -> dict[str, Any]:
        """Kubernetes Pod一覧を取得します。

        namespace指定を強く推奨します。
        未指定時は全namespaceの全Podを返すため応答が巨大になります。
        """
        return await k8s.list_pods(namespace)

    @tool
    async def k8s_get_pod(name: str, namespace: str = "default") -> dict[str, Any]:
        """指定したPodの詳細情報を取得します。"""
        return await k8s.get_pod(name, namespace)

    @tool
    async def k8s_get_pod_logs(
        name: str,
        namespace: str = "default",
        container: str = "",
        tail: int = 100,
    ) -> dict[str, Any]:
        """指定したPodのログを取得します。tailで取得行数を指定できます。"""
        return await k8s.get_pod_logs(name, namespace, container, tail)

    @tool
    async def k8s_list_events(namespace: str = "") -> dict[str, Any]:
        """Kubernetesイベント一覧を取得します。Warning/Errorイベントの確認に使用します。"""
        return await k8s.list_events(namespace)

    @tool
    async def k8s_list_namespaces() -> dict[str, Any]:
        """Kubernetes Namespace一覧を取得します。"""
        return await k8s.list_namespaces()

    @tool
    async def k8s_get_pods_top(namespace: str = "") -> dict[str, Any]:
        """各PodのCPU/メモリ使用状況を取得します。"""
        return await k8s.get_pods_top(namespace)

    @tool
    async def k8s_get_resource(
        kind: str,
        name: str = "",
        namespace: str = "",
        api_version: str = "",
    ) -> dict[str, Any]:
        """汎用Kubernetesリソースを取得します。

        kindにDeployment,Service,PVC,NetworkPolicy等を指定します。
        nameを省略するとリスト取得になります。
        api_versionは自動推定されますが、明示指定も可能です
        (例: apps/v1, networking.k8s.io/v1)。
        """
        return await k8s.get_resource(kind, name, namespace, api_version)

    return [
        k8s_list_pods,
        k8s_get_pod,
        k8s_get_pod_logs,
        k8s_list_events,
        k8s_list_namespaces,
        k8s_get_pods_top,
        k8s_get_resource,
    ]
