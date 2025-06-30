## Overview

This bash script provides a utility for extracting and exporting unmanaged Kubernetes resources from a specified namespace. It's designed to identify and export only those resources that are not managed by operators, Helm charts, or other automated systems, making it ideal for migration scenarios, backup operations, or infrastructure auditing.

## Purpose

The script addresses the common DevOps challenge of distinguishing between manually created resources and those managed by automation tools. It exports clean, reusable YAML manifests that can be version-controlled, migrated to other clusters, or used for disaster recovery purposes.

## Key Features

- **Intelligent Resource Filtering**: Automatically identifies and excludes resources managed by Helm, operators, or other controllers
- **Comprehensive Cleanup**: Removes cluster-specific metadata, runtime fields, and managed fields to produce clean, portable YAML
- **Multi-tool Compatibility**: Supports both `yq` and fallback sed/awk for YAML processing
- **Organized Output**: Creates timestamped directories with properly structured resource files
- **Context-aware**: Supports multiple Kubernetes contexts for multi-cluster environments

## Usage

```bash
./export-unmanaged-resources.sh <namespace> [context]
```

**Parameters:**
- `namespace` (required): Target Kubernetes namespace to export
- `context` (optional): Kubernetes context to use (defaults to current context)

**Example:**
```bash
./export-unmanaged-resources.sh production-app
./export-unmanaged-resources.sh staging-app my-staging-cluster
```

## Resource Types Exported

The script exports the following resource types when they are unmanaged:
- ConfigMaps and Secrets
- Services
- Deployments, StatefulSets, DaemonSets
- Jobs and CronJobs
- PersistentVolumeClaims
- ServiceAccounts
- RBAC resources (Roles, RoleBindings)
- NetworkPolicies and Ingresses
- HorizontalPodAutoscalers

## Managed object Detection Logic

Resources are considered "managed" and excluded if they:
- Contain Helm-related labels (`helm.sh/chart`, `app.kubernetes.io/managed-by: Helm`)
- Have `ownerReferences` pointing to controllers or operators
- Contain operator-specific labels or annotations
- Are system-generated (e.g., default ServiceAccount, kube-system resources)

## Output Structure

The script creates a timestamped directory containing:
- `00-namespace.yaml`: Namespace definition
- `<resource-type>.yaml`: Individual files for each resource type with unmanaged resources

## Prerequisites

- `kubectl` configured with appropriate cluster access
- `yq` (recommended) or standard Unix tools (sed, awk) for YAML processing
- Bash shell environment

## Use Cases

- **Cluster Migration**: Export unmanaged resources for migration to new clusters
- **Backup Operations**: Create portable backups of manually created resources
- **Infrastructure Auditing**: Identify resources not under version control or automation
- **Disaster Recovery**: Maintain clean manifests for critical unmanaged components

This script is particularly valuable in environments transitioning from manual resource management to GitOps or when performing cluster upgrades that require resource recreation.
