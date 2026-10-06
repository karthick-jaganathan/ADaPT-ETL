"""
A generic pipeline framework over the adapt CLI: named declarative DAGs (config/pipelines/<name>.yaml) order the nodes,
a per-node wrapper builds each node's `adapt run` command from the network's entry (config/networks.yaml) and the
source's declared spec, and an account lookup (accounts.py) drives runs triggered by user. adapt is never imported:
every node shells out to it.
"""

from adapt.orchestration.settings import (EXECUTION_MODES, K8S_REQUIRED_SECRET_KEYS, K8S_SETTINGS, PROJECT_DIR,
                                          REPO_ROOT, adapt_bin, adapt_image, docker_bin, execution_mode, k8s_settings,
                                          networks_path, pipeline_spec_override, pipelines_dir, runs_dir,
                                          warehouse_dir)

__all__ = ["EXECUTION_MODES", "K8S_REQUIRED_SECRET_KEYS", "K8S_SETTINGS", "PROJECT_DIR", "REPO_ROOT", "adapt_bin",
           "adapt_image", "docker_bin", "execution_mode", "k8s_settings", "networks_path", "pipeline_spec_override",
           "pipelines_dir", "runs_dir", "warehouse_dir"]
