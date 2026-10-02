{{- define "infra.labels" -}}
app.kubernetes.io/part-of: barber
app.kubernetes.io/managed-by: {{ .Release.Service }}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version }}
{{- end -}}

{{- define "infra.postgresSecret" -}}
{{- default "postgres-credentials" .Values.postgres.existingSecret -}}
{{- end -}}
