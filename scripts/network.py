#!/usr/bin/env python3
"""Generate PostgreSQL backup network policies."""

from copy import deepcopy


def generate_ingress_policy(instance, cronjob):

    namespace = instance["namespace"]
    name = cronjob["metadata"]["name"]

    selector = instance.get("pod_selector") or {}

    if not (
        selector.get("matchLabels")
        or selector.get("matchExpressions")
    ):
        raise RuntimeError(
            f"Empty PostgreSQL selector: {namespace}/{name}"
        )

    backup_labels = (
        cronjob["spec"]["jobTemplate"]["spec"]
        ["template"]["metadata"]["labels"]
    )

    target = backup_labels["backup.turing.io/target"]

    if not target:
        raise RuntimeError("Missing backup target label")

    return {
        "apiVersion": "networking.k8s.io/v1",
        "kind": "NetworkPolicy",
        "metadata": {
            "name": f"{name}-db-ingress",
            "namespace": namespace,
            "labels": {
                "app.kubernetes.io/name":
                    "postgres-s3-backup",
                "app.kubernetes.io/component":
                    "network-policy",
                "backup.turing.io/target":
                    target
            }
        },
        "spec": {
            "podSelector": deepcopy(selector),
            "policyTypes": ["Ingress"],
            "ingress": [
                {
                    "from": [
                        {
                            "podSelector": {
                                "matchLabels": {
                                    "app.kubernetes.io/name":
                                        "postgres-s3-backup",
                                    "app.kubernetes.io/component":
                                        "backup",
                                    "backup.turing.io/target":
                                        target
                                }
                            }
                        }
                    ],
                    "ports": [
                        {
                            "protocol": "TCP",
                            "port": 5432
                        }
                    ]
                }
            ]
        }
    }
