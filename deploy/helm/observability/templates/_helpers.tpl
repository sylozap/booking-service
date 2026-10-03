{{- define "observability.labels" -}}
app.kubernetes.io/part-of: barber
app.kubernetes.io/managed-by: {{ .Release.Service }}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version }}
{{- end -}}

{{/*
The container settings Tempo, Loki and Alloy share: nothing gained beyond
what the image starts with.
*/}}
{{- define "observability.containerSecurity" -}}
allowPrivilegeEscalation: false
capabilities:
  drop: [ALL]
{{- end -}}
