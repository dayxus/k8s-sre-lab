{{/*
Naming and label helpers.

The chart deliberately reproduces the same object graph as manifests/base, including
the five recommended `app.kubernetes.io/*` labels plus `managed-by`, because the
Python test suite asserts the same invariants on kustomize output and on `helm template`
output. Keep the two in sync: tests/test_manifests.py and tests/test_chart.py share
the checks in scripts/invariants.py.
*/}}

{{- define "k8s-sre-lab.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "k8s-sre-lab.namespace" -}}
{{- default .Release.Namespace .Values.namespace.name -}}
{{- end -}}

{{/*
Full label set. Call as: {{ include "k8s-sre-lab.labels" (dict "ctx" . "component" "api") }}
*/}}
{{- define "k8s-sre-lab.labels" -}}
app.kubernetes.io/name: {{ include "k8s-sre-lab.name" .ctx }}
app.kubernetes.io/instance: {{ .ctx.Release.Name }}
app.kubernetes.io/version: {{ .ctx.Chart.AppVersion | quote }}
app.kubernetes.io/component: {{ .component }}
app.kubernetes.io/part-of: {{ .ctx.Values.partOf }}
app.kubernetes.io/managed-by: {{ .ctx.Release.Service }}
{{- end -}}

{{- define "k8s-sre-lab.selectorLabels" -}}
app.kubernetes.io/name: {{ include "k8s-sre-lab.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}

{{/*
Scheduling selectors (topology spread, pod anti-affinity).

Identical to the block in manifests/base/topology-spread.yaml: it uses the two labels
that carry the same value in the kustomize and the Helm rendering (the release instance
and the component), so tests/test_chart.py can compare the rendered pod spec of both
delivery paths field by field. `app.kubernetes.io/name` cannot be used: kustomize names
the workload `demo-api`, Helm names it after the chart.
*/}}
{{- define "k8s-sre-lab.podSelectorLabels" -}}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/component: api
{{- end -}}

{{- define "k8s-sre-lab.podLabels" -}}
{{ include "k8s-sre-lab.selectorLabels" . }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/component: api
app.kubernetes.io/part-of: {{ .Values.partOf }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end -}}
