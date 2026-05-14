# ArgoCD Service Mode Checker

A lightweight health-check service that runs in the `argocd` namespace and reports which ArgoCD Applications have auto-sync or self-heal disabled — i.e. are in "service mode".

Use it to catch forgotten maintenance overrides before they cause drift.

## Why

When ArgoCD Applications are managed by ApplicationSets with `ignoreApplicationDifferences` on `/spec/syncPolicy`, operators can disable auto-sync at runtime without a git commit. That's useful during incidents, but it's easy to forget to re-enable afterwards. This pod gives you a single HTTP endpoint to check whether all apps are back to production mode.

## How it works

The pod queries the Kubernetes API for all `Application` CRs in the argocd namespace and inspects each one's `spec.syncPolicy.automated` field. It returns:

- **`200 OK`** — every Application has `selfHeal` and `prune` enabled
- **`409 Conflict`** — one or more Applications are missing auto-sync, selfHeal, or prune; the response body lists them
- **`500 Internal Server Error`** — couldn't reach the Kubernetes API

## Endpoints

| Path | Description |
|---|---|
| `GET /status` | Main check — returns sync policy status for all apps |
| `GET /` | Alias for `/status` |
| `GET /healthz` | Liveness/readiness probe (always returns `200 ok`) |

## Response examples

All apps healthy:

```json
{
  "status": "ok",
  "message": "All applications have auto-sync and selfHeal enabled.",
  "totalApps": 12
}
```

Apps in service mode:

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

The `serviceMode` object appears only if the Application carries `service-mode/entered-at` and `service-mode/reason` annotations (see [Entering service mode](#entering-service-mode) below).

## Prerequisites

- A Kubernetes cluster with ArgoCD installed
- Applications managed by ApplicationSets with `ignoreApplicationDifferences` configured for `/spec/syncPolicy` (otherwise the ApplicationSet controller overwrites runtime patches)

## Deployment

### 1. Build the image

```bash
cd app/
docker build -t registry.example.com/argocd-service-mode-checker:latest .
docker push registry.example.com/argocd-service-mode-checker:latest
```

### 2. Update the image reference

Edit `k8s/manifests.yaml` and replace `registry.example.com/argocd-service-mode-checker:latest` with your actual registry path.

### 3. Apply manifests

```bash
kubectl apply -f k8s/manifests.yaml
```

This creates:

- A `ServiceAccount` with read-only access to `applications.argoproj.io`
- A `ClusterRole` and `ClusterRoleBinding` (minimal RBAC — only `get` and `list` on Applications)
- A `Deployment` running the checker pod
- A `ClusterIP` Service on port 80

### 4. (Optional) Prometheus monitoring

```bash
kubectl apply -f k8s/monitoring.yaml
```

This adds a Prometheus `Probe` and a `PrometheusRule` that fires `ArgoCD_ServiceModeActive` if the endpoint returns non-200 for more than 30 minutes. Requires prometheus-operator and blackbox-exporter.

## Configuration

Environment variables on the Deployment:

| Variable | Default | Description |
|---|---|---|
| `PORT` | `8080` | HTTP listen port |
| `ARGOCD_NAMESPACE` | `argocd` | Namespace to query for Application CRs |
| `LABEL_SELECTOR` | *(empty)* | Optional Kubernetes label selector to scope which Applications are checked (e.g. `app.kubernetes.io/instance=my-appset`) |

## Entering service mode

Disable auto-sync on an Application at runtime:

```bash
# Annotate for audit trail (optional but recommended)
kubectl annotate application <app-name> -n argocd \
  service-mode/entered-at="$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
  service-mode/reason="investigating pod crashloop" \
  --overwrite

# Disable auto-sync
kubectl patch application <app-name> -n argocd --type merge -p '{
  "spec": { "syncPolicy": { "automated": null } }
}'
```

## Exiting service mode

Re-enable auto-sync:

```bash
kubectl patch application <app-name> -n argocd --type merge -p '{
  "spec": {
    "syncPolicy": {
      "automated": { "selfHeal": true, "prune": true }
    }
  }
}'

# Clean up annotations
kubectl annotate application <app-name> -n argocd \
  service-mode/entered-at- \
  service-mode/reason-
```

Or delete the Application and let the ApplicationSet recreate it with the template defaults:

```bash
kubectl delete application <app-name> -n argocd
```

## Testing the endpoint

From inside the cluster:

```bash
curl -s service-mode-checker.argocd.svc/status | jq .
```

From your workstation (port-forward):

```bash
kubectl port-forward -n argocd svc/service-mode-checker 8080:80
curl -s localhost:8080/status | jq .
```

## ApplicationSet setup

For the runtime patching to survive ApplicationSet reconciliation, add `ignoreApplicationDifferences` to your ApplicationSet:

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
  generators:
    - git:
        repoURL: https://git.example.com/deploy-configs.git
        revision: HEAD
        directories:
          - path: apps/*
  template:
    spec:
      syncPolicy:
        automated:
          selfHeal: true
          prune: true
```

This tells the ApplicationSet controller not to overwrite `syncPolicy` if it has been changed on the live Application. Without it, the controller will revert your runtime patches on its next reconciliation cycle.

## Project structure

```
├── app/
│   ├── main.py          # Python HTTP server (stdlib only, no dependencies)
│   └── Dockerfile        # Alpine-based image
└── k8s/
    ├── manifests.yaml    # ServiceAccount, RBAC, Deployment, Service
    └── monitoring.yaml   # Prometheus Probe + alert rule (optional)
```

## Security

- Runs as non-root (UID 1000)
- Read-only root filesystem
- All Linux capabilities dropped
- RBAC limited to read-only access on Application CRs
- No external dependencies — Python stdlib only