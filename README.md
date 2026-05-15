# ArgoCD Status Checker

A lightweight in-cluster HTTP service that monitors ArgoCD Application health, sync status, and auto-sync configuration — giving you simple HTTP endpoints to poll or alert on.

## Endpoints

| Path | Description |
|---|---|
| `GET /healthz` | Liveness/readiness probe (always `200 ok`) |
| `GET /auto-sync` | `200` if all apps have auto-sync + selfHeal enabled, `409` if any are in service mode |
| `GET /apps` | `200` if all apps are Healthy + Synced, `409` listing any with issues |

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
      "issues": ["auto-sync disabled"],
      "serviceMode": {
        "enteredAt": "2026-05-14T08:30:00Z",
        "reason": "investigating pod crashloop"
      }
    },
    {
      "name": "my-app-backend",
      "issues": ["selfHeal disabled"]
    }
  ]
}
```

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

The `serviceMode` object appears on any affected app that carries `service-mode/entered-at` and `service-mode/reason` annotations.

## Prerequisites

- A Kubernetes cluster with ArgoCD installed
- For auto-sync runtime patching to survive ApplicationSet reconciliation, add `ignoreApplicationDifferences` for `/spec/syncPolicy` to your ApplicationSet (see below)

## Deployment

### 1. Apply manifests

```bash
kubectl apply -f k8s/manifests.yaml
```

This creates:

- A `ServiceAccount` with read-only access to `applications.argoproj.io`
- A `ClusterRole` and `ClusterRoleBinding` (minimal RBAC — only `get` and `list` on Applications)
- A `Deployment` running the checker pod
- A `ClusterIP` Service on port 80

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
| `LABEL_SELECTOR` | *(empty)* | Optional label selector to scope which Applications are checked |

## Service mode annotations

Annotate apps when entering service mode for audit trail visibility in the API responses:

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
