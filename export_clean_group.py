#!/usr/bin/env python3
"""
Kubernetes Resource Grouper

This script exports Kubernetes resources grouped by their workloads (deployments, statefulsets, cronjobs).
For each workload, it discovers and exports all related resources including:
- ConfigMaps and Secrets referenced by the workload
- PersistentVolumeClaims used by the workload
- Services that match the workload's selector
- Ingresses/Routes that point to those services
- ServiceAccounts used by the workload
- HPAs that target the workload
- NetworkPolicies that apply to the workload

Only unmanaged resources (not managed by Helm or operators) are exported.
Each workload gets its own directory with all related resources as clean YAML files.
"""

import os
import sys
import subprocess
import datetime
import yaml
import argparse
import json
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed
from collections import defaultdict

# -----------------------------
# Configuration Constants
# -----------------------------

# Workload resources that we'll use as the primary grouping mechanism
WORKLOAD_RESOURCES = ["deployments", "statefulsets", "cronjobs", "jobs"]

# Related resources that might be associated with workloads
RELATED_RESOURCES = [
    "configmaps",
    "secrets", 
    "services",
    "persistentvolumeclaims",
    "serviceaccounts",
    "roles",
    "rolebindings",
    "clusterroles",
    "clusterrolebindings",
    "ingresses",
    "routes",  # OpenShift routes
    "networkpolicies",
    "horizontalpodautoscalers",
]

# Rules for cleaning up Kubernetes YAML files by removing runtime/managed fields
CLEAN_RULES = {
    # Metadata fields that are managed by Kubernetes and should be removed
    "metadata": [
        "creationTimestamp",     # When the resource was created
        "deletionGracePeriodSeconds", 
        "deletionTimestamp",
        "generation",            # Resource generation number
        "managedFields",         # Server-side apply fields
        "resourceVersion",       # Internal resource version
        "selfLink",              # Deprecated API field
        "uid",                   # Unique identifier
        "finalizers",            # Cleanup hooks
        "ownerReferences",       # Parent-child relationships
    ],
    # Annotations that are added by Kubernetes/operators and should be removed
    "annotations": [
        "kubectl.kubernetes.io/last-applied-configuration",  # kubectl apply history
        "olm.operatorNamespace", # Operator Lifecycle Manager fields
        "olm.operatorGroup", 
        "volume.kubernetes.io/selected-node",               # Volume scheduling
        "pv.kubernetes.io/bind-completed",
        "pv.kubernetes.io/bound-by-controller",
        "volume.beta.kubernetes.io/storage-provisioner",
        "volume.kubernetes.io/storage-provisioner",
    ],
    # Spec fields that are auto-assigned and should be removed
    "spec": [
        "clusterIP",                    # Auto-assigned service IPs
        "clusterIPs", 
        "ipFamilies",
        "ipFamilyPolicy",
        "sessionAffinityConfig",
        "externalIPs",
        "externalTrafficPolicy",
        "healthCheckNodePort",
        "loadBalancerIP",
        "loadBalancerSourceRanges",
        "publishNotReadyAddresses",
        "volumeName",                   # Auto-assigned PVC volume name
    ],
}
# -----------------------------


def run_cmd(cmd):
    """
    Execute a shell command and return its stdout.
    
    Args:
        cmd (str): Shell command to execute
        
    Returns:
        str: Command stdout if successful, empty string if failed
        
    This function safely executes kubectl commands and handles errors gracefully.
    """
    try:
        result = subprocess.run(
            cmd, shell=True, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE
        )
        if result.returncode != 0:
            return ""
        return result.stdout.strip()
    except Exception:
        return ""


class K8sResourceGrouper:
    """
    Main class for grouping Kubernetes resources by their workloads.
    
    This class discovers workloads (deployments, statefulsets, cronjobs) in a namespace
    and finds all related resources that belong to each workload. It then exports
    clean YAML files grouped by workload in separate directories.
    
    Attributes:
        namespace (str): Target Kubernetes namespace
        context (str): Kubernetes context to use
        dry_run (bool): If True, only preview without creating files
        workers (int): Number of parallel workers for processing
        export_dir (str): Directory name for exported files
        summary (dict): Summary of exported resources per workload
        all_resources (dict): Cache of all resources in the namespace
    """
    
    def __init__(self, namespace, context=None, dry_run=False, workers=10):
        self.namespace = namespace
        self.context = context
        self.dry_run = dry_run
        self.workers = workers
        self.export_dir = f"{namespace}-grouped-{datetime.datetime.now().strftime('%Y-%m-%d_%H-%M-%S')}"
        self.summary = defaultdict(list)
        self.all_resources = {}  # Cache for all resources

    def switch_context(self):
        """Switch to the specified Kubernetes context if provided."""
        if self.context:
            print(f"Switching to context: {self.context}")
            run_cmd(f"kubectl config use-context {self.context}")

    def namespace_exists(self):
        """
        Check if the target namespace exists.
        
        Returns:
            bool: True if namespace exists, False otherwise
        """
        return bool(run_cmd(f"kubectl get namespace {self.namespace}"))

    def is_managed(self, resource, name):
        """
        Check if a resource is managed by Helm, operators, or has owner references.
        
        Managed resources are typically created and maintained by tools like Helm
        or Kubernetes operators, so we skip them to focus on manually created resources.
        
        Args:
            resource (str): Resource type (e.g., 'deployments')
            name (str): Resource name
            
        Returns:
            bool: True if resource is managed, False if it's manually created
        """
        labels = run_cmd(
            f"kubectl get {resource} {name} -n {self.namespace} -o jsonpath='{{.metadata.labels}}'"
        )
        owners = run_cmd(
            f"kubectl get {resource} {name} -n {self.namespace} -o jsonpath='{{.metadata.ownerReferences}}'"
        )
        return ("helm.sh/chart" in labels or "app.kubernetes.io/managed-by" in labels or owners.strip())

    def get_resource_yaml(self, resource, name):
        """
        Fetch and parse a Kubernetes resource as YAML.
        
        Args:
            resource (str): Resource type
            name (str): Resource name
            
        Returns:
            dict: Parsed YAML object, or None if failed
        """
        yaml_str = run_cmd(f"kubectl get {resource} {name} -n {self.namespace} -o yaml")
        if not yaml_str.strip():
            return None
        try:
            return yaml.safe_load(yaml_str)
        except Exception as e:
            print(f"[WARN] Could not parse YAML for {resource}/{name}: {e}")
            return None

    def cache_all_resources(self):
        """
        Cache all resources in the namespace for efficient lookup.
        
        This method fetches all resources once and stores them in memory,
        avoiding repeated kubectl calls during processing. Only unmanaged
        resources that we care about are cached.
        
        Note: ClusterRoles and ClusterRoleBindings are cluster-scoped, so we fetch them without namespace.
        """
        print("Caching all resources...")
        
        # Handle namespace-scoped resources
        namespace_scoped = [r for r in WORKLOAD_RESOURCES + RELATED_RESOURCES 
                           if r not in ['clusterroles', 'clusterrolebindings']]
        
        for resource_type in namespace_scoped:
            self.all_resources[resource_type] = {}
            # Get all resource names of this type in the namespace
            names = run_cmd(f"kubectl get {resource_type} -n {self.namespace} -o jsonpath='{{.items[*].metadata.name}}'")
            if names:
                for name in names.split():
                    # Skip system defaults and managed resources
                    if self.should_skip_resource(resource_type, name):
                        continue
                    
                    # Skip resources managed by Helm/operators
                    if self.is_managed(resource_type, name):
                        continue
                        
                    # Fetch and cache the resource YAML
                    resource_obj = self.get_resource_yaml(resource_type, name)
                    if resource_obj:
                        self.all_resources[resource_type][name] = resource_obj

    def get_cluster_resource_yaml(self, resource, name):
        """
        Fetch and parse a cluster-scoped Kubernetes resource as YAML.
        
        Args:
            resource (str): Resource type (e.g., 'clusterroles', 'clusterrolebindings')
            name (str): Resource name
            
        Returns:
            dict: Parsed YAML object, or None if failed
        """
        yaml_str = run_cmd(f"kubectl get {resource} {name} -o yaml")
        if not yaml_str.strip():
            return None
        try:
            return yaml.safe_load(yaml_str)
        except Exception as e:
            print(f"[WARN] Could not parse YAML for {resource}/{name}: {e}")
            return None

    def should_skip_cluster_resource(self, resource_type, name):
        """
        Determine if we should skip a cluster-scoped resource.
        
        We skip system cluster resources that are built into Kubernetes.
        
        Args:
            resource_type (str): Type of resource
            name (str): Name of resource
            
        Returns:
            bool: True if resource should be skipped
        """
        # Skip system cluster roles and bindings
        system_prefixes = [
            "system:",
            "cluster-admin",
            "admin",
            "edit", 
            "view",
            "kubeadm:",
            "node-",
            "kubernetes-",
        ]
        
        if any(name.startswith(prefix) for prefix in system_prefixes):
            return True
            
        return False
        
        # Handle cluster-scoped resources (ClusterRoles and ClusterRoleBindings)
        for resource_type in ['clusterroles', 'clusterrolebindings']:
            self.all_resources[resource_type] = {}
            # Get all cluster-scoped resources (no namespace)
            names = run_cmd(f"kubectl get {resource_type} -o jsonpath='{{.items[*].metadata.name}}'")
            if names:
                for name in names.split():
                    # Skip system cluster resources
                    if self.should_skip_cluster_resource(resource_type, name):
                        continue
                        
                    # Fetch cluster resource YAML (no namespace)
                    resource_obj = self.get_cluster_resource_yaml(resource_type, name)
                    if resource_obj:
                        self.all_resources[resource_type][name] = resource_obj

    def should_skip_resource(self, resource_type, name):
        """
        Determine if we should skip a resource based on its type and name.
        
        We skip system resources that are automatically created by Kubernetes
        and don't represent user workloads or configurations.
        
        Args:
            resource_type (str): Type of resource
            name (str): Name of resource
            
        Returns:
            bool: True if resource should be skipped
        """
        # Skip default service account (exists in every namespace)
        if resource_type == "serviceaccounts" and name == "default":
            return True
        # Skip system configmaps/secrets
        if resource_type in ["configmaps", "secrets"] and (
            name.startswith("kube-") or          # Kubernetes system resources
            name.startswith("default-token-") or # Default service account tokens
            name.startswith("sh.helm.release")   # Helm release data
        ):
            return True
        return False

    def extract_referenced_resources(self, workload_obj, workload_type):
        """
        Extract all ConfigMaps, Secrets, PVCs, and ServiceAccounts referenced by a workload.
        
        This method analyzes the workload's pod template specification to find:
        - ConfigMaps and Secrets mounted as volumes or used in environment variables
        - PersistentVolumeClaims mounted as volumes
        - ServiceAccounts specified for the pods
        - For StatefulSets: PVCs created from volumeClaimTemplates
        
        Args:
            workload_obj (dict): The workload resource YAML
            workload_type (str): Type of workload (deployments, statefulsets, etc.)
            
        Returns:
            dict: Dictionary with sets of referenced resource names by type
        """
        referenced = {
            'configmaps': set(),
            'secrets': set(), 
            'persistentvolumeclaims': set(),
            'serviceaccounts': set()
        }
        
        # Extract pod template spec based on workload type
        # Different workload types have pod specs in different locations
        pod_spec = None
        if workload_type in ["deployments", "statefulsets"]:
            pod_spec = workload_obj.get('spec', {}).get('template', {}).get('spec', {})
        elif workload_type == "cronjobs":
            # CronJobs have nested structure: spec.jobTemplate.spec.template.spec
            pod_spec = workload_obj.get('spec', {}).get('jobTemplate', {}).get('spec', {}).get('template', {}).get('spec', {})
        elif workload_type == "jobs":
            pod_spec = workload_obj.get('spec', {}).get('template', {}).get('spec', {})
            
        if not pod_spec:
            return referenced

        # Extract service account (skip if it's the default one)
        sa = pod_spec.get('serviceAccountName') or pod_spec.get('serviceAccount')
        if sa and sa != 'default':
            referenced['serviceaccounts'].add(sa)

        # Extract resources from volume definitions
        for volume in pod_spec.get('volumes', []):
            if 'configMap' in volume:
                referenced['configmaps'].add(volume['configMap']['name'])
            elif 'secret' in volume:
                referenced['secrets'].add(volume['secret']['secretName'])
            elif 'persistentVolumeClaim' in volume:
                referenced['persistentvolumeclaims'].add(volume['persistentVolumeClaim']['claimName'])

        # Extract resources from container environment variables
        for container in pod_spec.get('containers', []) + pod_spec.get('initContainers', []):
            # Check individual environment variables
            for env in container.get('env', []):
                if 'valueFrom' in env:
                    if 'configMapKeyRef' in env['valueFrom']:
                        referenced['configmaps'].add(env['valueFrom']['configMapKeyRef']['name'])
                    elif 'secretKeyRef' in env['valueFrom']:
                        referenced['secrets'].add(env['valueFrom']['secretKeyRef']['name'])
            
            # Check environment variable sources (envFrom)
            for env_from in container.get('envFrom', []):
                if 'configMapRef' in env_from:
                    referenced['configmaps'].add(env_from['configMapRef']['name'])
                elif 'secretRef' in env_from:
                    referenced['secrets'].add(env_from['secretRef']['name'])

        # Handle StatefulSet volumeClaimTemplates
        # StatefulSets can create PVCs dynamically based on templates
        if workload_type == "statefulsets":
            for vct in workload_obj.get('spec', {}).get('volumeClaimTemplates', []):
                pvc_name = vct.get('metadata', {}).get('name')
                if pvc_name:
                    # StatefulSet PVCs are named: <template-name>-<statefulset-name>-<ordinal>
                    # We need to find actual PVCs that match this pattern
                    ss_name = workload_obj.get('metadata', {}).get('name')
                    for pvc_name_actual in self.all_resources.get('persistentvolumeclaims', {}):
                        if pvc_name_actual.startswith(f"{pvc_name}-{ss_name}-"):
                            referenced['persistentvolumeclaims'].add(pvc_name_actual)

        return referenced

    def find_matching_services(self, workload_obj):
        """
        Find services that match the workload's pod selector.
        
        Services select pods using label selectors. This method finds services
        whose selectors match the labels that the workload assigns to its pods.
        
        Args:
            workload_obj (dict): The workload resource YAML
            
        Returns:
            list: List of service names that select this workload's pods
        """
        # Get the labels that this workload assigns to its pods
        workload_labels = workload_obj.get('spec', {}).get('selector', {}).get('matchLabels', {})
        if not workload_labels:
            return []

        matching_services = []
        # Check each service to see if its selector matches our workload's pod labels
        for svc_name, svc_obj in self.all_resources.get('services', {}).items():
            svc_selector = svc_obj.get('spec', {}).get('selector', {})
            # A service matches if all of its selector labels are present in the workload's labels
            if svc_selector and all(workload_labels.get(k) == v for k, v in svc_selector.items()):
                matching_services.append(svc_name)
        
        return matching_services

    def find_matching_ingresses_and_routes(self, service_names):
        """
        Find ingresses and OpenShift routes that point to the given services.
        
        This method examines ingress and route configurations to find which ones
        route traffic to the services associated with our workload.
        
        Args:
            service_names (list): List of service names to look for
            
        Returns:
            list: List of tuples (resource_type, resource_name) for matching ingresses/routes
        """
        matching_resources = []
        
        # Check ingresses - they can have multiple rules and paths
        for ing_name, ing_obj in self.all_resources.get('ingresses', {}).items():
            for rule in ing_obj.get('spec', {}).get('rules', []):
                for path in rule.get('http', {}).get('paths', []):
                    # Check backend service name (depends on ingress API version)
                    backend_service = path.get('backend', {}).get('service', {}).get('name')
                    if backend_service in service_names:
                        matching_resources.append(('ingresses', ing_name))
                        break

        # Check OpenShift routes - they typically point to a single service
        for route_name, route_obj in self.all_resources.get('routes', {}).items():
            route_service = route_obj.get('spec', {}).get('to', {}).get('name')
            if route_service in service_names:
                matching_resources.append(('routes', route_name))

        return matching_resources

    def find_related_hpa(self, workload_name, workload_type):
        """
        Find HorizontalPodAutoscaler that targets this workload.
        
        HPAs scale workloads based on metrics. This method finds HPAs that
        specifically target our workload.
        
        Args:
            workload_name (str): Name of the workload
            workload_type (str): Type of workload (deployments, statefulsets, etc.)
            
        Returns:
            str: Name of matching HPA, or None if not found
        """
        for hpa_name, hpa_obj in self.all_resources.get('horizontalpodautoscalers', {}).items():
            target_ref = hpa_obj.get('spec', {}).get('scaleTargetRef', {})
            # Check if HPA targets our workload by name and kind
            if (target_ref.get('name') == workload_name and 
                target_ref.get('kind', '').lower() == workload_type.rstrip('s')):  # Remove 's' from plural
                return hpa_name
        return None

    def find_rbac_for_serviceaccount(self, sa_name):
        """
        Find Roles, RoleBindings, ClusterRoles, and ClusterRoleBindings associated with a ServiceAccount.
        
        This method searches for RBAC resources that grant permissions to the specified ServiceAccount.
        It looks for both namespace-scoped (Role/RoleBinding) and cluster-scoped (ClusterRole/ClusterRoleBinding) resources.
        
        Args:
            sa_name (str): Name of the ServiceAccount
            
        Returns:
            dict: Dictionary with lists of RBAC resource names by type
        """
        rbac_resources = {
            'roles': [],
            'rolebindings': [],
            'clusterroles': [],
            'clusterrolebindings': []
        }
        
        # Find RoleBindings that reference this ServiceAccount
        for rb_name, rb_obj in self.all_resources.get('rolebindings', {}).items():
            subjects = rb_obj.get('subjects', [])
            for subject in subjects:
                if (subject.get('kind') == 'ServiceAccount' and 
                    subject.get('name') == sa_name and
                    subject.get('namespace', self.namespace) == self.namespace):
                    rbac_resources['rolebindings'].append(rb_name)
                    
                    # Also get the Role that this RoleBinding references
                    role_ref = rb_obj.get('roleRef', {})
                    if role_ref.get('kind') == 'Role':
                        role_name = role_ref.get('name')
                        if role_name and role_name in self.all_resources.get('roles', {}):
                            if role_name not in rbac_resources['roles']:
                                rbac_resources['roles'].append(role_name)
                    elif role_ref.get('kind') == 'ClusterRole':
                        # RoleBinding can reference ClusterRole
                        cluster_role_name = role_ref.get('name')
                        if cluster_role_name and cluster_role_name not in rbac_resources['clusterroles']:
                            rbac_resources['clusterroles'].append(cluster_role_name)
        
        # Find ClusterRoleBindings that reference this ServiceAccount
        for crb_name, crb_obj in self.all_resources.get('clusterrolebindings', {}).items():
            subjects = crb_obj.get('subjects', [])
            for subject in subjects:
                if (subject.get('kind') == 'ServiceAccount' and 
                    subject.get('name') == sa_name and
                    subject.get('namespace', self.namespace) == self.namespace):
                    rbac_resources['clusterrolebindings'].append(crb_name)
                    
                    # Also get the ClusterRole that this ClusterRoleBinding references
                    role_ref = crb_obj.get('roleRef', {})
                    if role_ref.get('kind') == 'ClusterRole':
                        cluster_role_name = role_ref.get('name')
                        if cluster_role_name and cluster_role_name not in rbac_resources['clusterroles']:
                            rbac_resources['clusterroles'].append(cluster_role_name)
        
        return rbac_resources

    def find_related_networkpolicies(self, workload_obj):
        """
        Find network policies that might apply to this workload.
        
        NetworkPolicies use pod selectors to determine which pods they apply to.
        This method finds policies whose selectors match our workload's pod labels.
        
        Args:
            workload_obj (dict): The workload resource YAML
            
        Returns:
            list: List of NetworkPolicy names that apply to this workload
        """
        # Get the labels that this workload assigns to its pods
        workload_labels = workload_obj.get('spec', {}).get('template', {}).get('metadata', {}).get('labels', {})
        matching_policies = []
        
        # Check each network policy's pod selector
        for np_name, np_obj in self.all_resources.get('networkpolicies', {}).items():
            pod_selector = np_obj.get('spec', {}).get('podSelector', {}).get('matchLabels', {})
            # Policy applies if all of its selector labels match the workload's pod labels
            if pod_selector and all(workload_labels.get(k) == v for k, v in pod_selector.items()):
                matching_policies.append(np_name)
                
        return matching_policies
        """Find HPA that targets this workload"""
        for hpa_name, hpa_obj in self.all_resources.get('horizontalpodautoscalers', {}).items():
            target_ref = hpa_obj.get('spec', {}).get('scaleTargetRef', {})
            if (target_ref.get('name') == workload_name and 
                target_ref.get('kind', '').lower() == workload_type.rstrip('s')):  # Remove 's' from plural
                return hpa_name
        return None

    def find_related_networkpolicies(self, workload_obj):
        """Find network policies that might apply to this workload"""
        workload_labels = workload_obj.get('spec', {}).get('template', {}).get('metadata', {}).get('labels', {})
        matching_policies = []
        
        for np_name, np_obj in self.all_resources.get('networkpolicies', {}).items():
            pod_selector = np_obj.get('spec', {}).get('podSelector', {}).get('matchLabels', {})
            if pod_selector and all(workload_labels.get(k) == v for k, v in pod_selector.items()):
                matching_policies.append(np_name)
                
        return matching_policies

    def clean_yaml(self, obj):
        """
        Clean Kubernetes YAML object by removing runtime and managed fields.
        
        This method removes fields that are automatically managed by Kubernetes
        or contain runtime information, making the YAML suitable for redeployment
        in other clusters or environments.
        
        Args:
            obj (dict): Kubernetes resource object
            
        Returns:
            dict: Cleaned Kubernetes resource object
        """
        if not isinstance(obj, dict):
            return obj

        # Clean metadata section
        meta = obj.get("metadata", {})
        if isinstance(meta, dict):
            # Remove managed metadata fields
            for k in CLEAN_RULES["metadata"]:
                meta.pop(k, None)

            # Clean annotations by removing system-generated ones
            ann = meta.get("annotations", {})
            if isinstance(ann, dict):
                for a in CLEAN_RULES["annotations"]:
                    ann.pop(a, None)
                # Remove empty annotations section
                if not ann:
                    meta.pop("annotations", None)

            # Remove empty labels section
            labels = meta.get("labels", {})
            if isinstance(labels, dict) and not labels:
                meta.pop("labels", None)

        # Remove status section (contains runtime state)
        obj.pop("status", None)

        # Clean spec section
        spec = obj.get("spec", {})
        if isinstance(spec, dict):
            # Remove auto-assigned spec fields
            for k in CLEAN_RULES["spec"]:
                spec.pop(k, None)
            # Remove auto-assigned nodePorts from services
            if "ports" in spec and isinstance(spec["ports"], list):
                for p in spec["ports"]:
                    p.pop("nodePort", None)

        return obj

    def save_resource(self, workload_name, resource_type, resource_name, resource_obj):
        """
        Save a resource to the workload's directory as a clean YAML file.
        
        Each workload gets its own directory, and all related resources are saved
        as separate YAML files within that directory.
        
        Args:
            workload_name (str): Name of the workload (used as directory name)
            resource_type (str): Type of resource (e.g., 'deployments', 'services')
            resource_name (str): Name of the resource
            resource_obj (dict): The resource YAML object
        """
        workload_dir = Path(self.export_dir) / workload_name
        workload_dir.mkdir(parents=True, exist_ok=True)
        
        # Clean the resource object before saving
        cleaned_obj = self.clean_yaml(resource_obj.copy())
        out_file = workload_dir / f"{resource_type}-{resource_name}.yaml"
        
        try:
            with open(out_file, "w") as f:
                yaml.dump(cleaned_obj, f, sort_keys=False, default_flow_style=False)
        except Exception as e:
            print(f"[WARN] Could not save {resource_type}/{resource_name}: {e}")

    def process_workload(self, workload_type, workload_name, workload_obj):
        """
        Process a single workload and find all its related resources.
        
        This is the main processing method that:
        1. Saves the workload itself
        2. Finds all directly referenced resources (ConfigMaps, Secrets, PVCs, ServiceAccounts)
        3. Finds services that select this workload's pods
        4. Finds ingresses/routes that point to those services
        5. Finds HPAs that target this workload
        6. Finds NetworkPolicies that apply to this workload's pods
        
        Args:
            workload_type (str): Type of workload (deployments, statefulsets, etc.)
            workload_name (str): Name of the workload
            workload_obj (dict): The workload resource YAML object
        """
        if self.dry_run:
            print(f"Processing {workload_type}/{workload_name}")
        
        related_resources = []
        
        # Save the workload itself first
        if not self.dry_run:
            self.save_resource(workload_name, workload_type, workload_name, workload_obj)
        related_resources.append(f"{workload_type}/{workload_name}")

        # Extract directly referenced resources (ConfigMaps, Secrets, PVCs, ServiceAccounts)
        referenced = self.extract_referenced_resources(workload_obj, workload_type)
        
        for resource_type, resource_names in referenced.items():
            for resource_name in resource_names:
                if resource_name in self.all_resources.get(resource_type, {}):
                    if not self.dry_run:
                        self.save_resource(workload_name, resource_type, resource_name, 
                                         self.all_resources[resource_type][resource_name])
                    related_resources.append(f"{resource_type}/{resource_name}")

        # For each ServiceAccount, also find its RBAC resources
        for sa_name in referenced.get('serviceaccounts', []):
            if sa_name in self.all_resources.get('serviceaccounts', {}):
                rbac_resources = self.find_rbac_for_serviceaccount(sa_name)
                
                # Save RBAC resources
                for rbac_type, rbac_names in rbac_resources.items():
                    for rbac_name in rbac_names:
                        if rbac_name in self.all_resources.get(rbac_type, {}):
                            if not self.dry_run:
                                self.save_resource(workload_name, rbac_type, rbac_name,
                                                 self.all_resources[rbac_type][rbac_name])
                            related_resources.append(f"{rbac_type}/{rbac_name}")

        # Find services that select this workload's pods
        matching_services = self.find_matching_services(workload_obj)
        for svc_name in matching_services:
            if svc_name in self.all_resources.get('services', {}):
                if not self.dry_run:
                    self.save_resource(workload_name, 'services', svc_name,
                                     self.all_resources['services'][svc_name])
                related_resources.append(f"services/{svc_name}")

        # Find ingresses and routes that point to these services
        ing_routes = self.find_matching_ingresses_and_routes(matching_services)
        for resource_type, resource_name in ing_routes:
            if resource_name in self.all_resources.get(resource_type, {}):
                if not self.dry_run:
                    self.save_resource(workload_name, resource_type, resource_name,
                                     self.all_resources[resource_type][resource_name])
                related_resources.append(f"{resource_type}/{resource_name}")

        # Find HPA that targets this workload
        hpa_name = self.find_related_hpa(workload_name, workload_type)
        if hpa_name and hpa_name in self.all_resources.get('horizontalpodautoscalers', {}):
            if not self.dry_run:
                self.save_resource(workload_name, 'horizontalpodautoscalers', hpa_name,
                                 self.all_resources['horizontalpodautoscalers'][hpa_name])
            related_resources.append(f"horizontalpodautoscalers/{hpa_name}")

        # Find network policies that apply to this workload's pods
        netpol_names = self.find_related_networkpolicies(workload_obj)
        for np_name in netpol_names:
            if np_name in self.all_resources.get('networkpolicies', {}):
                if not self.dry_run:
                    self.save_resource(workload_name, 'networkpolicies', np_name,
                                     self.all_resources['networkpolicies'][np_name])
                related_resources.append(f"networkpolicies/{np_name}")

        # Store summary for final report
        self.summary[workload_name] = related_resources

    def helmify_folder(self, workload_name):
        if not self.dry_run:
            input_folder = os.path.join(self.export_dir, workload_name)
            output_folder = os.path.join(self.export_dir, workload_name+"-helmified")
            os.makedirs(output_folder, exist_ok=True)

            print(f"🛠️  Running helmify for '{workload_name}'")
            try:
                subprocess.run(["helmify", "-f", input_folder, output_folder], check=True)
            except subprocess.CalledProcessError as e:
                error_message = e.stderr or e.stdout or "Unknown error"
                print(f"❌ Helmify failed for '{workload_name}': {error_message}")

    def export_all(self):
        """
        Main export function that orchestrates the entire process.
        
        This method:
        1. Validates the namespace exists
        2. Creates the export directory
        3. Caches all resources for efficient processing
        4. Processes each workload and its related resources
        5. Prints a summary of what was exported
        """
        if not self.namespace_exists():
            print(f"❌ Namespace {self.namespace} does not exist!")
            sys.exit(1)

        print(f"Grouping resources by workloads in namespace: {self.namespace}")
        if not self.dry_run:
            Path(self.export_dir).mkdir(exist_ok=True)

        # Cache all resources first for efficient lookup
        self.cache_all_resources()

        # Process each workload and find its related resources
        for workload_type in WORKLOAD_RESOURCES:
            for workload_name, workload_obj in self.all_resources.get(workload_type, {}).items():
                self.process_workload(workload_type, workload_name, workload_obj)
                self.helmify_folder(workload_name)

        self.print_summary()

    def print_summary(self):
        """
        Print a summary of exported resources grouped by workload.
        
        This shows what was found and exported (or would be exported in dry-run mode).
        """
        print("\n✅ Export Summary:")
        if not self.summary:
            print("  No unmanaged workloads found.")
            return
            
        for workload_name, resources in self.summary.items():
            print(f"  📁 {workload_name} ({len(resources)} resources)")
            for resource in resources:
                print(f"      • {resource}")


def main():
    """
    Main entry point for the script.
    
    Parses command line arguments and executes the resource grouping process.
    """
    parser = argparse.ArgumentParser(
        description="Group Kubernetes resources by their workloads (deployments/statefulsets/cronjobs)"
    )
    parser.add_argument("namespace", help="Namespace to process")
    parser.add_argument("--context", help="Kubernetes context", default=None)
    parser.add_argument("--dry-run", action="store_true", help="Preview without saving files")
    parser.add_argument("--workers", type=int, default=10, help="Number of parallel workers")
    args = parser.parse_args()

    grouper = K8sResourceGrouper(
        namespace=args.namespace,
        context=args.context,
        dry_run=args.dry_run,
        workers=args.workers,
    )
    grouper.switch_context()
    grouper.export_all()


if __name__ == "__main__":
    main()
