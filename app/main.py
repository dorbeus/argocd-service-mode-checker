#!/usr/bin/env python3
"""
ArgoCD Service Mode Checker

Endpoints:
  GET /healthz    — liveness probe, always 200
  GET /auto-sync  — 200 if all apps have auto-sync+selfHeal, 409 if any are in service mode
  GET /apps       — 200 if all apps are Healthy+Synced, 409 listing any with issues

Returns:
  200 — all apps healthy/synced (or auto-sync enabled)
  409 — one or more apps have issues (JSON body lists them)
  500 — error talking to the Kubernetes API
"""

import json
import logging
import os
import signal
import sys
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.request import Request, urlopen
from urllib.error import URLError

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("service-mode-checker")

K8S_API = "https://kubernetes.default.svc"
NAMESPACE = os.environ.get("ARGOCD_NAMESPACE", "argocd")
TOKEN_PATH = "/var/run/secrets/kubernetes.io/serviceaccount/token"
CA_PATH = "/var/run/secrets/kubernetes.io/serviceaccount/ca.crt"

# Optional label selector to scope which Applications to check
LABEL_SELECTOR = os.environ.get("LABEL_SELECTOR", "")


def load_token():
    with open(TOKEN_PATH, "r") as f:
        return f.read().strip()


def get_applications():
    """Fetch all Application CRs from the argocd namespace."""
    import ssl

    token = load_token()
    url = (
        f"{K8S_API}/apis/argoproj.io/v1alpha1"
        f"/namespaces/{NAMESPACE}/applications"
    )
    if LABEL_SELECTOR:
        url += f"?labelSelector={LABEL_SELECTOR}"

    req = Request(url)
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Accept", "application/json")

    ctx = ssl.create_default_context(cafile=CA_PATH)
    with urlopen(req, context=ctx, timeout=10) as resp:
        return json.loads(resp.read())


def check_apps_health():
    """
    Returns (status_code, body_dict).
    200 = all apps Healthy+Synced, 409 = one or more have issues, 500 = error.
    """
    try:
        data = get_applications()
    except FileNotFoundError:
        return 500, {
            "status": "error",
            "message": "ServiceAccount token not found — is the pod running in-cluster?",
        }
    except URLError as e:
        return 500, {
            "status": "error",
            "message": f"Failed to query Kubernetes API: {e}",
        }
    except Exception as e:
        return 500, {
            "status": "error",
            "message": str(e),
        }

    apps = data.get("items", [])
    unhealthy_apps = []

    for app in apps:
        meta = app.get("metadata", {})
        name = meta.get("name", "<unknown>")
        app_status = app.get("status", {})
        health_status = app_status.get("health", {}).get("status", "Unknown")
        health_message = app_status.get("health", {}).get("message", "")
        sync_status = app_status.get("sync", {}).get("status", "Unknown")

        issues = []
        if health_status != "Healthy":
            issue = f"health: {health_status}"
            if health_message:
                issue += f" ({health_message})"
            issues.append(issue)
        if sync_status != "Synced":
            issues.append(f"sync: {sync_status}")

        if issues:
            entry = {
                "name": name,
                "issues": issues,
            }

            annotations = meta.get("annotations", {})
            entered_at = annotations.get("service-mode/entered-at")
            reason = annotations.get("service-mode/reason")
            if entered_at:
                entry["serviceMode"] = {
                    "enteredAt": entered_at,
                    "reason": reason or "",
                }

            unhealthy_apps.append(entry)

    if not unhealthy_apps:
        return 200, {
            "status": "ok",
            "message": "All applications are Healthy and Synced.",
            "totalApps": len(apps),
        }

    return 409, {
        "status": "unhealthy",
        "message": f"{len(unhealthy_apps)} of {len(apps)} application(s) have issues.",
        "totalApps": len(apps),
        "affectedApps": unhealthy_apps,
    }


def check_service_mode():
    """
    Returns (status_code, body_dict).
    200 = all good, 409 = apps in service mode, 500 = error.
    """
    try:
        data = get_applications()
    except FileNotFoundError:
        return 500, {
            "status": "error",
            "message": "ServiceAccount token not found — is the pod running in-cluster?",
        }
    except URLError as e:
        return 500, {
            "status": "error",
            "message": f"Failed to query Kubernetes API: {e}",
        }
    except Exception as e:
        return 500, {
            "status": "error",
            "message": str(e),
        }

    apps = data.get("items", [])
    service_mode_apps = []

    for app in apps:
        name = app.get("metadata", {}).get("name", "<unknown>")
        sync_policy = app.get("spec", {}).get("syncPolicy") or {}
        automated = sync_policy.get("automated")

        # Build detail about what's missing
        issues = []
        log.info(name + ": " + json.dumps(automated))
        if automated is None:
            issues.append("No automated sync policy defined")
        else:
            if automated.get("enabled", False) == False:
                issues.append("auto-sync disabled")
            if automated.get("selfHeal", False) == False:
                issues.append("selfHeal disabled")
            if automated.get("prune", False) == False:
                issues.append("prune disabled")

        if issues:
            entry = {
                "name": name,
                "issues": issues,
            }

            # Include service-mode annotations if present
            annotations = app.get("metadata", {}).get("annotations", {})
            entered_at = annotations.get("service-mode/entered-at")
            reason = annotations.get("service-mode/reason")
            if entered_at:
                entry["serviceMode"] = {
                    "enteredAt": entered_at,
                    "reason": reason or "",
                }

            service_mode_apps.append(entry)

    if not service_mode_apps:
        return 200, {
            "status": "ok",
            "message": "All applications have auto-sync and selfHeal enabled.",
            "totalApps": len(apps),
        }

    return 409, {
        "status": "service_mode_active",
        "message": f"{len(service_mode_apps)} of {len(apps)} application(s) are not in production mode.",
        "totalApps": len(apps),
        "affectedApps": service_mode_apps,
    }


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/healthz":
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(b"ok")
            return

        if self.path == "/auto-sync":
            code, body = check_service_mode()
            payload = json.dumps(body, indent=2)
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(payload.encode())
            return

        if self.path == "/apps":
            code, body = check_apps_health()
            payload = json.dumps(body, indent=2)
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(payload.encode())
            return

        self.send_response(404)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps({"error": "not found"}).encode())

    # Suppress default stderr logging per request
    def log_message(self, format, *args):
        log.info(format % args)


def shutdown_handler(signum, frame):
    log.info("Shutting down.")
    sys.exit(0)


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, shutdown_handler)
    signal.signal(signal.SIGINT, shutdown_handler)

    port = int(os.environ.get("PORT", "8080"))
    server = HTTPServer(("0.0.0.0", port), Handler)
    log.info(f"Listening on :{port}")
    server.serve_forever()
