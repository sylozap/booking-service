{{/*
The name of the service: the release name unless overridden.
*/}}
{{- define "service.name" -}}
{{- default .Release.Name .Values.nameOverride | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "service.selectorLabels" -}}
app.kubernetes.io/name: {{ include "service.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}

{{- define "service.labels" -}}
{{ include "service.selectorLabels" . }}
app.kubernetes.io/version: {{ .Values.image.tag | quote }}
app.kubernetes.io/part-of: barber
app.kubernetes.io/managed-by: {{ .Release.Service }}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version }}
{{- end -}}

{{- define "service.image" -}}
{{- $registry := required "image.registry is required" .Values.image.registry -}}
{{- $name := required "image.name is required" .Values.image.name -}}
{{- $tag := required "image.tag is required: an untagged image is \"latest\"" .Values.image.tag -}}
{{- printf "%s/%s:%s" $registry $name $tag -}}
{{- end -}}

{{- define "service.secretName" -}}
{{- default (printf "%s-env" (include "service.name" .)) .Values.secret.name -}}
{{- end -}}

{{/*
Variables taken one by one from the Secret of the service. Called with a
dict: the root context, and the keys to take.
*/}}
{{- define "service.secretEnv" -}}
{{- if not .keys -}}
{{- fail (printf "%s: secret.keys is empty; list the keys, or set secret.enabled=false" (include "service.name" .context)) -}}
{{- end -}}
{{- range .keys }}
- name: {{ . }}
  valueFrom:
    secretKeyRef:
      name: {{ include "service.secretName" $.context }}
      key: {{ . }}
{{- end }}
{{- end -}}

{{/*
Requests and limits of a container, failing the render when a limit is
missing. Called with a dict: resources, and what to name in the message.
*/}}
{{- define "service.resources" -}}
{{- $limits := (.resources | default dict).limits | default dict -}}
{{- $requests := (.resources | default dict).requests | default dict -}}
{{- if not (and $limits.cpu $limits.memory) -}}
{{- fail (printf "%s: resources.limits.cpu and resources.limits.memory are required" .owner) -}}
{{- end -}}
{{- if not (and $requests.cpu $requests.memory) -}}
{{- fail (printf "%s: resources.requests.cpu and resources.requests.memory are required" .owner) -}}
{{- end -}}
{{- toYaml .resources -}}
{{- end -}}
