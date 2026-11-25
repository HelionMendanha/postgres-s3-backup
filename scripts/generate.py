#!/usr/bin/env python3
"""Generate suspended PostgreSQL backup CronJobs."""

import argparse
import hashlib
import json
import re
import sys

from pathlib import Path
from datetime import datetime, timedelta


def load_json(path):
    with open(path, encoding="utf-8") as file:
        return json.load(file)


def env_value(name, value):
    return {
        "name": name,
        "value": str(value)
    }


def env_secret(name, secret, key):
    return {
        "name": name,
        "valueFrom": {
            "secretKeyRef": {
                "name": secret,
                "key": key
            }
        }
    }


def env_credential(name, credential):
    source = credential["type"]

    if source == "literal":
        return env_value(name, credential["value"])

    if source == "secretKeyRef":
        return {
            "name": name,
            "valueFrom": {
                "secretKeyRef": credential["reference"]
            }
        }

    if source == "configMapKeyRef":
        return {
            "name": name,
            "valueFrom": {
                "configMapKeyRef": credential["reference"]
            }
        }

    raise RuntimeError(
        f"Unsupported credential source: {name}"
    )


def resource_name(instance):
    identity = (
        f"{instance['namespace']}/"
        f"{instance['workload']}/"
        f"{instance['container']}"
    )

    suffix = hashlib.sha256(
        identity.encode()
    ).hexdigest()[:8]

    base = re.sub(
        r"[^a-z0-9-]+",
        "-",
        instance["workload"].lower()
    )

    return (
        f"pgbackup-{base[:34].strip('-')}-{suffix}"
    )


def schedule_for(config, index):
    start = datetime.strptime(
        config["backup"]["scheduleStart"],
        "%H:%M"
    )

    interval = config["backup"][
        "scheduleIntervalMinutes"
    ]

    scheduled = start + timedelta(
        minutes=index * interval
    )

    return (
        f"{scheduled.minute} "
        f"{scheduled.hour} * * *"
    )


def generate_cronjob(instance, config, index):

    namespace = instance["namespace"]
    workload = instance["workload"]

    environment = config[
        "namespace_environments"
    ][namespace]

    backup = config["backup"]
    storage = config["storage"]
    image = config["image"]
    security = config["security"]

    if len(instance["services"]) != 1:
        raise RuntimeError(
            f"Ambiguous service: {namespace}/{workload}"
        )

    service = instance["services"][0]
    credentials = instance["credentials"]

    password = credentials["POSTGRES_PASSWORD"]

    if password["type"] != "secretKeyRef":
        raise RuntimeError(
            f"Unsafe password source: {namespace}/{workload}"
        )

    digest = image.get("digest")

    image_ref = (
        f"{image['repository']}:{image['tag']}"
    )

    if digest:
        if not re.fullmatch(
            r"sha256:[a-f0-9]{64}", digest
        ):
            raise RuntimeError("Invalid image digest")

        image_ref += f"@{digest}"

    prefix = "/".join([
        config["provider"],
        config["cluster"],
        environment,
        namespace,
        workload
    ])

    name = resource_name(instance)

    labels = {
        "app.kubernetes.io/name":
            "postgres-s3-backup",
        "app.kubernetes.io/component":
            "backup",
        "app.kubernetes.io/instance":
            name,
        "backup.turing.io/target":
            name
    }

    env = [
        env_value("PGHOST", service["host"]),
        env_value("PGPORT", service["port"]),
        env_credential(
            "PGUSER",
            credentials["POSTGRES_USER"]
        ),
        env_credential("PGPASSWORD", password),
        env_value("PG_MAJOR", "auto"),
        env_value("PGCONNECT_TIMEOUT", 15),
        env_value(
            "PG_MAINTENANCE_DB",
            backup["maintenanceDatabase"]
        ),
        env_value(
            "EXCLUDE_DATABASES",
            ",".join(backup["excludeDatabases"])
        ),
        env_value("BACKUP_DRY_RUN", "true"),
        env_value("S3_ENDPOINT", storage["endpoint"]),
        env_value("S3_BUCKET", storage["bucket"]),
        env_value("S3_PREFIX", prefix),
        env_value(
            "S3_ADDRESSING_STYLE",
            storage["addressingStyle"]
        ),
        env_value(
            "AWS_DEFAULT_REGION",
            storage["region"]
        ),
        env_secret(
            "AWS_ACCESS_KEY_ID",
            storage["secretName"],
            storage["accessKeyField"]
        ),
        env_secret(
            "AWS_SECRET_ACCESS_KEY",
            storage["secretName"],
            storage["secretKeyField"]
        ),
        env_value("AWS_EC2_METADATA_DISABLED", "true"),
        env_value("BACKUP_DIR", "/backup"),
        env_value("TZ", "UTC")
    ]

    container = {
        "name": "postgres-backup",
        "image": image_ref,
        "imagePullPolicy": image["pullPolicy"],
        "command": [
            "python",
            "/app/scripts/backup_all.py"
        ],
        "env": env,
        "resources": config["resources"],
        "securityContext": {
            "allowPrivilegeEscalation": False,
            "readOnlyRootFilesystem": True,
            "capabilities": {
                "drop": ["ALL"]
            }
        },
        "volumeMounts": [
            {
                "name": "backup",
                "mountPath": "/backup"
            },
            {
                "name": "tmp",
                "mountPath": "/tmp"
            }
        ]
    }

    pod = {
        "restartPolicy": "Never",
        "automountServiceAccountToken": False,
        "securityContext": {
            "runAsNonRoot": True,
            "runAsUser": security["runAsUser"],
            "runAsGroup": security["runAsGroup"],
            "fsGroup": security["fsGroup"],
            "seccompProfile": {
                "type": "RuntimeDefault"
            }
        },
        "containers": [container],
        "volumes": [
            {
                "name": "backup",
                "emptyDir": {
                    "sizeLimit": "1Gi"
                }
            },
            {
                "name": "tmp",
                "emptyDir": {
                    "sizeLimit": "128Mi"
                }
            }
        ]
    }

    return {
        "apiVersion": "batch/v1",
        "kind": "CronJob",
        "metadata": {
            "name": name,
            "namespace": namespace,
            "labels": labels
        },
        "spec": {
            "schedule": schedule_for(config, index),
            "timeZone": backup["timezone"],
            "suspend": True,
            "concurrencyPolicy": "Forbid",
            "successfulJobsHistoryLimit":
                backup["successfulJobsHistoryLimit"],
            "failedJobsHistoryLimit":
                backup["failedJobsHistoryLimit"],
            "jobTemplate": {
                "spec": {
                    "backoffLimit": backup["backoffLimit"],
                    "activeDeadlineSeconds":
                        backup["activeDeadlineSeconds"],
                    "template": {
                        "metadata": {
                            "labels": labels
                        },
                        "spec": pod
                    }
                }
            }
        }
    }


def main():
    parser = argparse.ArgumentParser()

    parser.add_argument("--inventory", required=True)
    parser.add_argument("--config", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument(
        "--preview-all",
        action="store_true"
    )

    args = parser.parse_args()

    inventory = load_json(args.inventory)
    config = load_json(args.config)

    if inventory.get("status") != "success":
        raise RuntimeError("Invalid discovery inventory")

    if config["backup"]["enabled"] is not False:
        raise RuntimeError(
            "Active generation is not supported"
        )

    if config["backup"]["suspend"] is not True:
        raise RuntimeError("Suspension is mandatory")

    if config["backup"]["dryRun"] is not True:
        raise RuntimeError("Dry-run is mandatory")

    instances = [
        item for item in inventory["instances"]
        if item["status"] == "candidate"
        and (
            args.preview_all
            or item["backup_enabled"] is True
        )
    ]

    instances.sort(
        key=lambda item: (
            item["namespace"],
            item["workload"],
            item["container"]
        )
    )

    manifests = [
        generate_cronjob(item, config, index)
        for index, item in enumerate(instances)
    ]

    identities = [
        (
            item["metadata"]["namespace"],
            item["metadata"]["name"]
        )
        for item in manifests
    ]

    if len(set(identities)) != len(identities):
        raise RuntimeError("Duplicate resource name")

    result = {
        "apiVersion": "v1",
        "kind": "List",
        "items": manifests
    }

    output = Path(args.output)

    output.parent.mkdir(
        parents=True,
        exist_ok=True
    )

    output.write_text(
        json.dumps(result, indent=2) + "\n",
        encoding="utf-8"
    )

    print(json.dumps({
        "status": "success",
        "mode": "preview-only",
        "cronjobs_generated": len(manifests),
        "suspended": True,
        "dry_run": True,
        "output": str(output)
    }, indent=2))


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(
            f"GENERATION FAILED: {error}",
            file=sys.stderr
        )
        sys.exit(1)
