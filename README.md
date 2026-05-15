# ArgoCD Status Checker

A lightweight in-cluster HTTP service that monitors ArgoCD Application health, sync status, and auto-sync configuration. Exposes simple HTTP endpoints you can poll from CI, alerting, or dashboards.

## Endpoints

| Path | Description |
|---|---|
| `GET /healthz` | Liveness/readiness probe — always `200 ok` |
| `GET /auto-sync` | `200` if all apps have auto-sync, selfHeal, and prune enabled; `409` if any are missing |
| `GET /apps` | `200` if all apps are `Healthy` and `Synced`; `409` listing any with issues |

All endpoints return JSON. Both `/auto-sync` and `/apps` follow the same response structure.

## Response examples

### `GET /auto-sync`

All apps in production mode:

```json
{
  "status": "ok",
  "message": "All applications have auto-sync and selfHeal enabled.",
  "totalApps": 12
}
```

One or more apps in service mode:

```json
{
  "status": "service_mode_active",
  "message": "2 of 12 application(s) are not in production mode.",
  "totalApps": 12,
  "affectedApps": [
    {
      "name": "my-app-frontend",
      "issues": ["No automated sync policy defined"],
      "serviceMode": {
        "enteredAt": "2026-05-14T08:30:00Z",
        "reason": "investigating pod crashloop"
      }
    },
    {
      "name": "my-app-backend",
      "issues": ["selfHeal disabled", "prune disabled"]
    }
  ]
}
```

Possible issues per app: `No automated sync policy defined`, `auto-sync disabled`, `selfHeal disabled`, `prune disabled`.

### `GET /apps`

All apps healthy:

```json
{
  "status": "ok",
  "message": "All applications are Healthy and Synced.",
  "totalApps": 12
}
```

One or more apps with issues:

```json
{
  "status": "unhealthy",
  "message": "2 of 12 application(s) have issues.",
  "totalApps": 12,
  "affectedApps": [
    {
      "name": "my-app-frontend",
      "issues": ["health: Degraded (OOMKilled)"]
    },
    {
      "name": "my-app-backend",
      "issues": ["sync: OutOfSync"]
    }
  ]
}
```

Possible issues per app: `health: <status> (<message>)`, `sync: <status>`.

### Error response (both endpoints)

```json
{
  "status": "error",
  "message": "ServiceAccount token not found — is the pod running in-cluster?"
}
```

The `serviceMode` object appears on any affected app that carries `service-mode/entered-at` and `service-mode/reason` annotations (see [Service mode annotations](#service-mode-annotations)).

## Prerequisites

- Kubernetes cluster with ArgoCD installed
- For auto-sync runtime patches to survive ApplicationSet reconciliation, configure `ignoreApplicationDifferences` on your ApplicationSet (see [ApplicationSet setup](#applicationset-setup))

## Deployment

### 1. Apply manifests

```bash
kubectl apply -f k8s/manifests.yaml
```

Creates:

- `ServiceAccount` with read-only access to `applications.argoproj.io`
- `ClusterRole` + `ClusterRoleBinding` — only `get` and `list` on Applications
- `Deployment` running the checker pod
- `ClusterIP` Service on port 80

### 2. (Optional) Prometheus monitoring

```bash
kubectl apply -f k8s/monitoring.yaml
```

Adds a Prometheus `Probe` targeting both `/auto-sync` and `/apps`, plus two `PrometheusRule` alerts:

- `ArgoCD_ServiceModeActive` — fires if `/auto-sync` returns non-200 for 30+ minutes
- `ArgoCD_AppsUnhealthy` — fires if `/apps` returns non-200 for 30+ minutes

Requires prometheus-operator and blackbox-exporter.

## Configuration

| Variable | Default | Description |
|---|---|---|
| `PORT` | `8080` | HTTP listen port |
| `ARGOCD_NAMESPACE` | `argocd` | Namespace to query for Application CRs |
| `LABEL_SELECTOR` | *(empty)* | Optional label selector to scope which Applications are checked (e.g. `app.kubernetes.io/instance=my-appset`) |

## Service mode annotations

Annotate apps when disabling auto-sync to surface context in the `/auto-sync` response:

```bash
kubectl annotate application <app-name> -n argocd \
  service-mode/entered-at="$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
  service-mode/reason="investigating pod crashloop" \
  --overwrite
```

Remove when done:

```bash
kubectl annotate application <app-name> -n argocd \
  service-mode/entered-at- \
  service-mode/reason-
```

## ApplicationSet setup

To allow runtime sync policy changes to survive ApplicationSet reconciliation:

```yaml
apiVersion: argoproj.io/v1alpha1
kind: ApplicationSet
metadata:
  name: my-apps
  namespace: argocd
spec:
  ignoreApplicationDifferences:
    - jsonPointers:
        - /spec/syncPolicy
  generators: [...]
  template:
    spec:
      syncPolicy:
        automated:
          selfHeal: true
          prune: true
```

## Testing

From inside the cluster:

```bash
curl -s argocd-status-checker.argocd.svc/auto-sync | jq .
curl -s argocd-status-checker.argocd.svc/apps | jq .
```

From your workstation:

```bash
kubectl port-forward -n argocd svc/argocd-status-checker 8080:80
curl -s localhost:8080/auto-sync | jq .
curl -s localhost:8080/apps | jq .
```

## Project structure

```text
├── app/
│   ├── main.py          # Python HTTP server (stdlib only, no dependencies)
│   └── Dockerfile       # Alpine-based image
└── k8s/
    ├── manifests.yaml   # ServiceAccount, RBAC, Deployment, Service
    └── monitoring.yaml  # Prometheus Probe + alert rules (optional)
```

## Security

- Runs as non-root (UID 1000)
- Read-only root filesystem
- All Linux capabilities dropped
- RBAC limited to read-only access on Application CRs
- No external dependencies — Python stdlib only
