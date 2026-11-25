#!/usr/bin/env python3
"""Read-only Kubernetes PostgreSQL discovery."""

import argparse
import json
import re
import subprocess
import sys

IMAGE_PATTERN = re.compile(
    r"(^|/)(postgres|postgresql|pgvector)(:|@|/|$)",
    re.IGNORECASE
)


def kubectl_get(resource):
    result = subprocess.run(
        ["kubectl", "get", resource, "-A", "-o", "json"],
        capture_output=True,
        text=True,
        check=True
    )
    return json.loads(result.stdout)["items"]


def discover_credentials(container):
    credentials = {}

    for env in container.get("env", []):
        name = env.get("name")

        if name not in (
            "POSTGRES_USER",
            "POSTGRES_PASSWORD",
            "POSTGRES_DB"
        ):
            continue

        source = env.get("valueFrom", {})

        if "secretKeyRef" in source:
            credentials[name] = {
                "type": "secretKeyRef",
                "reference": source["secretKeyRef"]
            }

        elif "configMapKeyRef" in source:
            credentials[name] = {
                "type": "configMapKeyRef",
                "reference": source["configMapKeyRef"]
            }

        elif "value" in env:
            credentials[name] = {
                "type": "literal"
            }

            if name != "POSTGRES_PASSWORD":
                credentials[name]["value"] = env["value"]

        else:
            credentials[name] = {
                "type": "unsupported"
            }

    return credentials


def discover_services(namespace, labels, services):
    matches = []

    for service in services:
        metadata = service["metadata"]
        spec = service["spec"]

        if metadata["namespace"] != namespace:
            continue

        selector = spec.get("selector") or {}

        if not selector:
            continue

        if not all(
            labels.get(key) == value
            for key, value in selector.items()
        ):
            continue

        for port in spec.get("ports", []):
            if (
                port.get("port") == 5432
                and port.get("protocol", "TCP") == "TCP"
            ):
                matches.append({
                    "name": metadata["name"],
                    "host": (
                        f"{metadata['name']}.{namespace}.svc"
                    ),
                    "port": port["port"],
                    "headless": (
                        spec.get("clusterIP") == "None"
                    )
                })

    return matches


def discover(workloads, services, excluded):
    instances = []

    for workload in workloads:
        metadata = workload["metadata"]
        namespace = metadata["namespace"]

        if namespace in excluded:
            continue

        template = workload["spec"]["template"]
        labels = template.get("metadata", {}).get(
            "labels", {}
        )

        annotations = metadata.get("annotations") or {}

        for container in template["spec"]["containers"]:
            image = container["image"]

            if not IMAGE_PATTERN.search(image):
                continue

            candidates = discover_services(
                namespace,
                labels,
                services
            )

            credentials = discover_credentials(
                container
            )

            issues = []

            if len(candidates) != 1:
                issues.append(
                    "Missing or ambiguous PostgreSQL service"
                )

            password = credentials.get(
                "POSTGRES_PASSWORD", {}
            )

            if password.get("type") != "secretKeyRef":
                issues.append(
                    "Password secret reference requires review"
                )

            replicas = workload["spec"].get(
                "replicas", 1
            )

            if replicas != 1:
                issues.append(
                    "Replica topology requires review"
                )

            instances.append({
                "namespace": namespace,
                "kind": workload["kind"],
                "workload": metadata["name"],
                "container": container["name"],
                "image": image,
                "replicas": replicas,
                "backup_enabled": (
                    annotations.get(
                        "backup.turing.io/enabled",
                        "false"
                    ).lower() == "true"
                ),
                "services": candidates,
                "credentials": credentials,
                "issues": issues,
                "status": (
                    "review-required"
                    if issues
                    else "candidate"
                )
            })

    return sorted(
        instances,
        key=lambda item: (
            item["namespace"],
            item["workload"],
            item["container"]
        )
    )


def main():
    parser = argparse.ArgumentParser()

    parser.add_argument(
        "--exclude-namespace",
        action="append",
        default=[]
    )

    args = parser.parse_args()

    excluded = {
        "kube-system",
        "kube-public",
        "kube-node-lease"
    }

    excluded.update(args.exclude_namespace)

    workloads = kubectl_get(
        "statefulsets,deployments"
    )

    services = kubectl_get("services")

    instances = discover(
        workloads,
        services,
        excluded
    )

    print(json.dumps({
        "status": "success",
        "mode": "discovery-only",
        "instances_discovered": len(instances),
        "namespaces_discovered": len({
            item["namespace"]
            for item in instances
        }),
        "instances": instances
    }, indent=2))


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(
            f"DISCOVERY FAILED: {error}",
            file=sys.stderr
        )
        sys.exit(1)
