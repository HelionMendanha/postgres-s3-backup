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


def validated_cidrs(values):
    from ipaddress import ip_network

    if not values:
        raise RuntimeError("Missing network CIDRs")

    result = []

    for value in values:
        network = ip_network(value, strict=True)

        if network.prefixlen == 0:
            raise RuntimeError(
                f"Unrestricted CIDR is not allowed: {value}"
            )

        result.append(str(network))

    return result


def generate_egress_policy(instance, cronjob, config):

    from ipaddress import ip_address

    namespace = instance["namespace"]
    name = cronjob["metadata"]["name"]

    network = config["network"]
    dns = network["dns"]
    s3 = network["s3"]

    selector = instance.get("pod_selector") or {}

    if not (
        selector.get("matchLabels")
        or selector.get("matchExpressions")
    ):
        raise RuntimeError("Missing PostgreSQL selector")

    if len(instance["services"]) != 1:
        raise RuntimeError("Ambiguous PostgreSQL service")

    service = instance["services"][0]

    dns_selector = dns["podSelector"]

    if not (
        dns_selector.get("matchLabels")
        or dns_selector.get("matchExpressions")
    ):
        raise RuntimeError("Missing DNS pod selector")

    backup_labels = (
        cronjob["spec"]["jobTemplate"]["spec"]
        ["template"]["metadata"]["labels"]
    )

    backup_selector = {
        "matchLabels": {
            "app.kubernetes.io/name":
                "postgres-s3-backup",
            "app.kubernetes.io/component":
                "backup",
            "backup.turing.io/target":
                backup_labels["backup.turing.io/target"]
        }
    }

    postgres_destinations = [
        {"podSelector": deepcopy(selector)}
    ]

    for value in service.get("cluster_ips", []):

        if value in ("None", "", None):
            continue

        address = ip_address(value)

        postgres_destinations.append({
            "ipBlock": {
                "cidr": (
                    f"{address}/{address.max_prefixlen}"
                )
            }
        })

    dns_destinations = [
        {
            "namespaceSelector": {
                "matchLabels": {
                    "kubernetes.io/metadata.name":
                        dns["namespace"]
                }
            },
            "podSelector": deepcopy(dns_selector)
        }
    ]

    dns_destinations.extend([
        {"ipBlock": {"cidr": cidr}}
        for cidr in validated_cidrs(dns["cidrs"])
    ])

    s3_destinations = [
        {"ipBlock": {"cidr": cidr}}
        for cidr in validated_cidrs(s3["cidrs"])
    ]

    s3_port = int(s3.get("port", 443))

    if not 1 <= s3_port <= 65535:
        raise RuntimeError("Invalid S3 port")

    return {
        "apiVersion": "networking.k8s.io/v1",
        "kind": "NetworkPolicy",
        "metadata": {
            "name": f"{name}-egress",
            "namespace": namespace,
            "labels": {
                "app.kubernetes.io/name":
                    "postgres-s3-backup",
                "app.kubernetes.io/component":
                    "network-policy",
                "backup.turing.io/target": name
            }
        },
        "spec": {
            "podSelector": backup_selector,
            "policyTypes": ["Egress"],
            "egress": [
                {
                    "to": postgres_destinations,
                    "ports": [
                        {
                            "protocol": "TCP",
                            "port": 5432
                        }
                    ]
                },
                {
                    "to": dns_destinations,
                    "ports": [
                        {
                            "protocol": "UDP",
                            "port": 53
                        },
                        {
                            "protocol": "TCP",
                            "port": 53
                        }
                    ]
                },
                {
                    "to": s3_destinations,
                    "ports": [
                        {
                            "protocol": "TCP",
                            "port": s3_port
                        }
                    ]
                }
            ]
        }
    }
