# GitOps Preparation Tool

A Python script that exports Kubernetes resources grouped by their workloads (deployments, statefulsets, cronjobs). It automatically discovers and groups related resources like ConfigMaps, Secrets, Services, Ingresses, and more.

## What It Does

For each workload in a namespace, the script finds and exports:
- **The workload itself** (Deployment, StatefulSet, CronJob, Job)
- **ConfigMaps and Secrets** referenced by the workload
- **PersistentVolumeClaims** used by the workload
- **Services** that select the workload's pods
- **Ingresses/Routes** that point to those services
- **ServiceAccounts** used by the workload (excluding default)
- **HorizontalPodAutoscalers** that target the workload
- **NetworkPolicies** that apply to the workload's pods

## Features

- ✅ **Smart Resource Discovery** - Automatically traces relationships between resources
- ✅ **Clean YAML Output** - Removes runtime fields and Kubernetes-managed metadata
- ✅ **Unmanaged Resources Only** - Skips Helm-managed and operator-managed resources
- ✅ **OpenShift Support** - Handles both Ingresses and OpenShift Routes
- ✅ **Dry Run Mode** - Preview what would be exported without creating files
- ✅ **Organized Output** - Each workload gets its own directory

## Requirements

- Python 3.6+
- `kubectl` configured and accessible
- Access to the target Kubernetes cluster
- Required Python packages: `pyyaml` (usually included in most Python installations)

## Installation

1. Save the script as `export_clean_group.py`
2. Make it executable:
   ```bash
   chmod +x export_clean_group.py
   ```

## Usage

### Basic Usage
```bash
python3 export_clean_group.py my-namespace
```

### Preview Without Creating Files
```bash
python3 export_clean_group.py my-namespace --dry-run
```

### Use Specific Kubernetes Context
```bash
python3 export_clean_group.py my-namespace --context my-cluster-context
```

### Command Line Options
```
positional arguments:
  namespace             Namespace to process

optional arguments:
  --context CONTEXT     Kubernetes context to use
  --dry-run            Preview without saving files
  --workers WORKERS    Number of parallel workers (default: 10)
  --help               Show help message
```

## Output Structure

The script creates a timestamped directory with subdirectories for each workload:

```
my-namespace-grouped-2024-01-15_14-30-45/
├── my-app-deployment/
│   ├── deployments-my-app-deployment.yaml
│   ├── services-my-app-service.yaml
│   ├── configmaps-my-app-config.yaml
│   ├── secrets-my-app-secret.yaml
│   ├── ingresses-my-app-ingress.yaml
│   └── serviceaccounts-my-app-sa.yaml
├── my-worker-cronjob/
│   ├── cronjobs-my-worker-cronjob.yaml
│   └── configmaps-worker-config.yaml
└── my-database-statefulset/
    ├── statefulsets-my-database-statefulset.yaml
    ├── services-my-database-service.yaml
    ├── persistentvolumeclaims-data-my-database-0.yaml
    └── persistentvolumeclaims-data-my-database-1.yaml
```

## What Gets Skipped

The script automatically skips:
- **Managed Resources**: Resources with Helm labels (`helm.sh/chart`) or managed-by annotations
- **System Resources**: Default service accounts, kube-system resources, etc.
- **Operator-Managed**: Resources with owner references (managed by controllers/operators)

## Example Output

```
$ python3 export_clean_group.py sample-app --dry-run

Caching all resources...
Processing deployments/frontend
Processing deployments/backend
Processing cronjobs/cleanup-job

✅ Export Summary:
  📁 frontend (5 resources)
      • deployments/frontend
      • configmaps/frontend-config
      • secrets/frontend-secret
      • services/frontend-service
      • ingresses/frontend-ingress
  📁 backend (4 resources)
      • deployments/backend
      • configmaps/backend-config
      • services/backend-service
      • horizontalpodautoscalers/backend-hpa
  📁 cleanup-job (2 resources)
      • cronjobs/cleanup-job
      • configmaps/cleanup-config
```

## Use Cases

- **Cluster Migration**: Export workloads for deployment in other clusters
- **Backup**: Create clean backups of application configurations
- **Documentation**: Understand resource relationships and dependencies
- **Development**: Extract production configs for local development
- **Troubleshooting**: Analyze complete application stacks

## Troubleshooting

### "Namespace does not exist"
Make sure you're connected to the right cluster and the namespace exists:
```bash
kubectl get namespaces
kubectl config current-context
```

### "No unmanaged workloads found"
This means all workloads in the namespace are managed by Helm or operators. Use `--dry-run` to see what's being skipped.

### Permission Errors
Ensure your kubectl context has read permissions for all resource types in the target namespace.

## Contributing

Feel free to submit issues or pull requests to improve the script.

Contact:

www.linkedin.com/in/yaron-yadid
