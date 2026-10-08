"""
A generic pipeline framework over the streamwright CLI: named declarative DAGs (config/pipelines/<name>.yaml) order the nodes,
a per-node wrapper builds each node's `streamwright run` command from the network's entry (config/networks.yaml) and the
source's declared spec, and an account lookup (accounts.py) drives runs triggered by user. streamwright is never imported:
every node shells out to it.
"""

from streamwright.orchestration.settings import (EXECUTION_MODES, K8S_REQUIRED_SECRET_KEYS, K8S_SETTINGS, PROJECT_DIR,
                                          REPO_ROOT, streamwright_bin, streamwright_image, docker_bin, execution_mode, k8s_settings,
                                          networks_path, pipeline_spec_override, pipelines_dir, runs_dir,
                                          warehouse_dir)

__all__ = ["EXECUTION_MODES", "K8S_REQUIRED_SECRET_KEYS", "K8S_SETTINGS", "PROJECT_DIR", "REPO_ROOT", "streamwright_bin",
           "streamwright_image", "docker_bin", "execution_mode", "k8s_settings", "networks_path", "pipeline_spec_override",
           "pipelines_dir", "runs_dir", "warehouse_dir"]
