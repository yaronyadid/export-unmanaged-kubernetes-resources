#!/bin/bash
# Export unmanaged Kubernetes resources (not managed by operators or Helm)

set -e

if [ $# -lt 1 ]; then
    echo "Usage: $0 <namespace> [context]"
    exit 1
fi

NAMESPACE=$1
CONTEXT=${2:-""}

if [ -n "$CONTEXT" ]; then
    kubectl config use-context "$CONTEXT"
fi

# Validate namespace exists
kubectl get namespace "$NAMESPACE" >/dev/null 2>&1 || {
    echo "Error: Namespace '$NAMESPACE' does not exist"
    exit 1
}

EXPORT_DIR="${NAMESPACE}-unmanaged-$(date +%Y%m%d-%H%M%S)"
mkdir -p "$EXPORT_DIR"

echo "Exporting unmanaged resources from namespace: $NAMESPACE"
echo "Export directory: $EXPORT_DIR"

# Function to clean YAML files
cleanup_yaml() {
    local file=$1

    if [ -f "$file" ] && [ -s "$file" ]; then
        if command -v yq >/dev/null 2>&1; then
            # Clean YAML using yq
            yq 'del(
              .metadata.creationTimestamp,
              .metadata.deletionGracePeriodSeconds,
              .metadata.deletionTimestamp,
              .metadata.generation,
              .metadata.managedFields,
              .metadata.resourceVersion,
              .metadata.selfLink,
              .metadata.uid,
              .metadata.finalizers,
              .metadata.ownerReferences,
              .metadata.annotations."kubectl.kubernetes.io/last-applied-configuration",
              .metadata.annotations."olm.operatorNamespace",
              .metadata.annotations."olm.operatorGroup",
              .metadata.annotations."volume.kubernetes.io/selected-node",
              .metadata.annotations."pv.kubernetes.io/bind-completed",
              .metadata.annotations."pv.kubernetes.io/bound-by-controller",
              .metadata.annotations."volume.beta.kubernetes.io/storage-provisioner",
              .metadata.annotations."volume.kubernetes.io/storage-provisioner",
              .status,
              .spec.clusterIP,
              .spec.clusterIPs,
              .spec.ipFamilies,
              .spec.ipFamilyPolicy,
              .spec.sessionAffinityConfig,
              .spec.externalIPs,
              .spec.externalTrafficPolicy,
              .spec.healthCheckNodePort,
              .spec.loadBalancerIP,
              .spec.loadBalancerSourceRanges,
              .spec.publishNotReadyAddresses,
              .spec.ports[].nodePort,
              .spec.volumeName
            ) |
            del(.metadata.annotations | select(. == {})) |
            del(.metadata.labels | select(. == {}))' "$file" > "${file}.cleaned" && mv "${file}.cleaned" "$file"
        else
            echo "Warning: 'yq' not found. Falling back to basic cleanup with sed/awk."

            # Remove metadata and spec fields using sed
            sed -i.bak '
                /resourceVersion:/d
                /uid:/d
                /selfLink:/d
                /creationTimestamp:/d
                /deletionGracePeriodSeconds:/d
                /deletionTimestamp:/d
                /generation:/d
                /observedGeneration:/d
                /finalizers:/d
                /ownerReferences:/d
                /kubectl.kubernetes.io\/last-applied-configuration:/d
                /olm.operatorNamespace:/d
                /olm.operatorGroup:/d
                /volume.kubernetes.io\/selected-node:/d
                /pv.kubernetes.io\/bind-completed:/d
                /pv.kubernetes.io\/bound-by-controller:/d
                /volume.beta.kubernetes.io\/storage-provisioner:/d
                /volume.kubernetes.io\/storage-provisioner:/d
                /managedFields:/,/^[^ ]/d
                /clusterIP:/d
                /clusterIPs:/d
                /ipFamilies:/d
                /ipFamilyPolicy:/d
                /sessionAffinityConfig:/,/^[^ ]/d
                /externalIPs:/d
                /externalTrafficPolicy:/d
                /healthCheckNodePort:/d
                /loadBalancerIP:/d
                /loadBalancerSourceRanges:/d
                /publishNotReadyAddresses:/d
                /nodePort:/d
                /volumeName:/d
            ' "$file"

            # Remove status sections using awk
            awk '
                BEGIN { skip=0 }
                /^status:/ { skip=1; next }
                /^[^[:space:]]/ { skip=0 }
                !skip { print }
            ' "$file.bak" > "$file"

            rm -f "$file.bak"
        fi

        # Remove if file ends up empty
        if [ ! -s "$file" ]; then
            rm -f "$file"
            return 1
        fi
    else
        rm -f "$file"
        return 1
    fi

    return 0
}

# Function to check if resource is managed
is_managed_resource() {
    local resource_type=$1
    local resource_name=$2

    # Check if managed by Helm
    if kubectl get "$resource_type" "$resource_name" -n "$NAMESPACE" -o jsonpath='{.metadata.labels}' 2>/dev/null | grep -q "helm.sh/chart\|app.kubernetes.io/managed-by.*Helm"; then
        return 0
    fi

    # Check if managed by operator (has ownerReferences or operator labels)
    if kubectl get "$resource_type" "$resource_name" -n "$NAMESPACE" -o jsonpath='{.metadata.ownerReferences}' 2>/dev/null | grep -q .; then
        return 0
    fi

    # Check for common operator labels
    if kubectl get "$resource_type" "$resource_name" -n "$NAMESPACE" -o jsonpath='{.metadata.labels}' 2>/dev/null | grep -E "operator|controller|app.kubernetes.io/managed-by" | grep -v "kubectl\|Helm" >/dev/null; then
        return 0
    fi

    return 1
}

# Export namespace definition
echo "Exporting namespace definition..."
kubectl get namespace "$NAMESPACE" -o yaml > "$EXPORT_DIR/00-namespace.yaml"
cleanup_yaml "$EXPORT_DIR/00-namespace.yaml"

# Resources to export (excluding managed and irrelevant ones)
UNMANAGED_RESOURCES=(
    "configmaps"
    "secrets"
    "services"
    "deployments"
    "statefulsets"
    "daemonsets"
    "jobs"
    "cronjobs"
    "persistentvolumeclaims"
    "serviceaccounts"
    "roles"
    "rolebindings"
    "networkpolicies"
    "ingresses"
    "horizontalpodautoscalers"
)

for resource in "${UNMANAGED_RESOURCES[@]}"; do
    echo "Processing $resource..."

    # Get all resource names
    resource_names=$(kubectl get "$resource" -n "$NAMESPACE" -o jsonpath='{.items[*].metadata.name}' 2>/dev/null || echo "")

    if [ -n "$resource_names" ]; then
        temp_file="$EXPORT_DIR/temp_${resource}.yaml"
        final_file="$EXPORT_DIR/${resource}.yaml"

        > "$final_file"  # Create empty file

        for name in $resource_names; do
            # Skip default service account and system-managed resources
            if [ "$resource" = "serviceaccounts" ] && [ "$name" = "default" ]; then
                continue
            fi

            if [ "$resource" = "configmaps" ] || [ "$resource" = "secrets" ]; then
                if echo "$name" | grep -E "^(kube-|default-token-|sh\.helm\.release)" >/dev/null; then
                    continue
                fi
            fi

            # Check if resource is managed
            if ! is_managed_resource "$resource" "$name"; then
                echo "  Exporting unmanaged $resource: $name"
                kubectl get "$resource" "$name" -n "$NAMESPACE" -o yaml >> "$temp_file" 2>/dev/null || continue
                echo "---" >> "$temp_file"
            else
                echo "  Skipping managed $resource: $name"
            fi
        done

        if [ -f "$temp_file" ]; then
            mv "$temp_file" "$final_file"
            cleanup_yaml "$final_file"
        fi
    fi
done
