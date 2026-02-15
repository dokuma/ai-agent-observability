{{/*
チャートのフルネーム（リリース名-チャート名）。63文字制限。
*/}}
{{- define "ai-agent-monitoring.fullname" -}}
{{- if contains .Chart.Name .Release.Name }}
{{- .Release.Name | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- printf "%s-%s" .Release.Name .Chart.Name | trunc 63 | trimSuffix "-" }}
{{- end }}
{{- end }}

{{/*
チャート名
*/}}
{{- define "ai-agent-monitoring.name" -}}
{{- .Chart.Name | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
チャートラベル（chart + version）
*/}}
{{- define "ai-agent-monitoring.chartLabel" -}}
{{- printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
共通ラベル
*/}}
{{- define "ai-agent-monitoring.labels" -}}
helm.sh/chart: {{ include "ai-agent-monitoring.chartLabel" . }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
app.kubernetes.io/part-of: {{ include "ai-agent-monitoring.name" . }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
{{- end }}

{{/*
セレクタラベル（component パラメータ付き）
Usage: {{ include "ai-agent-monitoring.selectorLabels" (dict "context" . "component" "agent") }}
*/}}
{{- define "ai-agent-monitoring.selectorLabels" -}}
app.kubernetes.io/name: {{ include "ai-agent-monitoring.name" .context }}
app.kubernetes.io/instance: {{ .context.Release.Name }}
app.kubernetes.io/component: {{ .component }}
{{- end }}

{{/*
コンテナイメージ構築
Usage: {{ include "ai-agent-monitoring.image" (dict "global" .Values.global "image" .Values.agent.image) }}
*/}}
{{- define "ai-agent-monitoring.image" -}}
{{- printf "%s/%s/%s:%s" .global.imageRegistry .global.imageNamespace .image.name .image.tag }}
{{- end }}

{{/*
Secret 名を返す（existingSecret があればそれを使用）
*/}}
{{- define "ai-agent-monitoring.secretName" -}}
{{- if .Values.secrets.existingSecret }}
{{- .Values.secrets.existingSecret }}
{{- else }}
{{- printf "%s-secret" (include "ai-agent-monitoring.fullname" .) }}
{{- end }}
{{- end }}

{{/*
ConfigMap 名
*/}}
{{- define "ai-agent-monitoring.configmapName" -}}
{{- printf "%s-config" (include "ai-agent-monitoring.fullname" .) }}
{{- end }}

{{/*
ServiceAccount 名
*/}}
{{- define "ai-agent-monitoring.serviceAccountName" -}}
{{- if .Values.serviceAccount.name }}
{{- .Values.serviceAccount.name }}
{{- else }}
{{- include "ai-agent-monitoring.fullname" . }}
{{- end }}
{{- end }}
