# Copyright 2021 Agnostiq Inc.
#
# This file is part of Covalent.
#
# Licensed under the GNU Affero General Public License 3.0 (the "License").
# A copy of the License may be obtained with this software package or at
#
#      https://www.gnu.org/licenses/agpl-3.0.en.html
#
# Use of this file is prohibited except in compliance with the License. Any
# modifications or derivative works of this file must retain this copyright
# notice, and modified files must contain a notice indicating that they have
# been altered from the originals.
#
# Covalent is distributed in the hope that it will be useful, but WITHOUT
# ANY WARRANTY; without even the implied warranty of MERCHANTABILITY or
# FITNESS FOR A PARTICULAR PURPOSE. See the License for more details.
#
# Relief from the License may be granted by purchasing a commercial license.

"""Kubernetes executor plugin for the Covalent dispatcher."""

import base64
import os
import shutil
import subprocess
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import cloudpickle as pickle
import docker
import toml
from covalent._shared_files.logger import app_log
from covalent.executor import BaseExecutor
from kubernetes import client, config
from kubernetes.client.rest import ApiException

_EXECUTOR_PLUGIN_DEFAULTS = {
    "base_image": "python:3.8-slim-bullseye",
    "k8s_config_file": os.path.join(os.environ["HOME"], ".kube/config"),
    "k8s_context": "",
    "image_repo": "covalent-eks-task",
    "registry": "localhost",
    "registry_credentials_file": "",
    "data_store": "",
    "vcpu": "500m",
    "memory": "1G",
    "cache_dir": os.path.join(os.environ["HOME"], ".cache/covalent"),
    "poll_freq": 10,
}

executor_plugin_name = "KubernetesExecutor"

# TODO: Update docstrings


class KubernetesExecutor(BaseExecutor):
    """Kubernetes executor plugin class."""

    def __init__(
        self,
        base_image: str,
        k8s_config_file: str,
        k8s_context: str,
        image_repo: str,
        registry: str,
        registry_credentials_file: str,
        data_store: str,
        poll_freq: int,
        vcpu: str,
        memory: str,
        **kwargs,
    ):
        super().__init__(**kwargs)

        self.base_image = base_image
        self.k8s_config_file = k8s_config_file
        self.k8s_context = k8s_context
        self.image_repo = image_repo
        self.registry = registry
        self.registry_credentials_file = registry_credentials_file
        self.data_store = data_store
        self.poll_freq = poll_freq
        self.vcpu = vcpu
        self.memory = memory

    def run(self, function: callable, args: List, kwargs: Dict):
        """Submit the function to a Kubernetes cluster."""

        run_id = str(uuid.uuid4())
        result_filename = f"result-{run_id}.pkl"
        image_tag = f"{run_id}"
        container_name = f"covalent-task-{image_tag}"
        job_name = f"job-{run_id}"
        docker_working_dir = "/data"

        app_log.debug(f"Run ID: {run_id}")

        # Load Kubernetes config file
        config.load_kube_config(self.k8s_config_file)

        # Validate the context
        contexts, active_context = config.list_kube_config_contexts()
        contexts = [context["name"] for context in contexts]

        if self.k8s_context not in contexts:
            raise ValueError(
                f"Context {self.k8s_context} was not found in the Kubernetes config file."
            )

        # Create the client
        api_client = config.new_client_from_config(context=self.k8s_context)

        # Create the cache directory used for storing pickles and metadata
        Path(self.cache_dir).mkdir(parents=True, exist_ok=True)

        # Containerize the task and perform any necessary transfers
        image_uri = self._package_and_upload(
            function,
            args,
            kwargs,
            self.base_image,
            image_tag,
            docker_working_dir,
            result_filename,
        )

        volumes = (
            [
                client.V1Volume(
                    name="local-mount",
                    host_path=client.V1HostPathVolumeSource(path=docker_working_dir),
                )
            ]
            if self.data_store.startswith("/")
            else []
        )
        mounts = (
            [client.V1VolumeMount(mount_path=docker_working_dir, name="local-mount")]
            if self.data_store.startswith("/")
            else []
        )
        pull_policy = "Never" if image_uri.startswith(self.image_repo) else ""

        container = client.V1Container(
            name=container_name,
            image=image_uri,
            image_pull_policy=pull_policy,
            volume_mounts=mounts,
            resources=client.V1ResourceRequirements(
                requests={
                    "cpu": self.vcpu,
                    "memory": self.memory,
                }
            ),
        )

        pod_template = client.V1PodTemplateSpec(
            spec=client.V1PodSpec(
                containers=[container],
                volumes=volumes,
                restart_policy="Never",
            )
        )

        metadata = client.V1ObjectMeta(name=job_name)

        job = client.V1Job(
            api_version="batch/v1",
            kind="Job",
            metadata=metadata,
            spec=client.V1JobSpec(backoff_limit=0, template=pod_template),
        )

        batch_api = client.BatchV1Api(api_client=api_client)
        batch_api.create_namespaced_job("default", job)

        self._poll_task(api_client, job_name)

        result = self._query_result(result_filename, image_tag)

        return result

    def _format_exec_script(
        self,
        func_filename: str,
        result_filename: str,
        docker_working_dir: str,
    ) -> str:
        """Create an executable Python script which executes the task.

        Args:
            func_filename: Name of the pickled function.
            result_filename: Name of the pickled result.
            docker_working_dir: Name of the working directory in the container.

        Returns:
            script: String object containing the executable Python script.
        """

        # Execution preamble
        exec_script = """
import os
import cloudpickle as pickle

local_func_filename = os.path.join("{docker_working_dir}", "{func_filename}")
local_result_filename = os.path.join("{docker_working_dir}", "{result_filename}")

        """.format(
            docker_working_dir=docker_working_dir,
            func_filename=func_filename,
            result_filename=result_filename,
        )

        # Pull from data store
        if self.data_store.startswith("s3://"):
            exec_script += """
import boto3
s3 = boto3.client("s3")
s3.download_file("{s3_bucket_name}", "{func_filename}", local_func_filename)
            """.format(
                func_filename=func_filename,
                s3_bucket_name=self.data_store[5:].split("/")[0],
            )

        # Extract and execute the task
        exec_script += """

with open(local_func_filename, "rb") as f:
    function, args, kwargs = pickle.load(f)

result = function(*args, **kwargs)

with open(local_result_filename, "wb") as f:
    pickle.dump(result, f)

        """

        # Push to data store
        if self.data_store.startswith("s3://"):
            exec_script += """
s3.upload_file(local_result_filename, "{s3_bucket_name}", "{result_filename}")
            """.format(
                result_filename=result_filename,
                s3_bucket_name=self.data_store[5:].split("/")[0],
            )

        return exec_script

    def _format_dockerfile(
        self, exec_script_filename: str, docker_working_dir: str, base_image: str
    ) -> str:
        """Create a Dockerfile which wraps an executable Python task.

        Args:
            exec_script_filename: Name of the executable Python script.
            docker_working_dir: Name of the working directory in the container.
            base_image: Name of the base image on which to build the task image.

        Returns:
            dockerfile: String object containing a Dockerfile.
        """

        # TODO: Including pre-release covalent is problematic
        dockerfile = """
FROM {base_image}

RUN pip install --no-cache-dir cloudpickle==2.0.0 boto3==1.20.48

RUN pip install --pre covalent

WORKDIR {docker_working_dir}

COPY {func_basename} {docker_working_dir}

ENTRYPOINT [ "python" ]
CMD [ "{docker_working_dir}/{func_basename}" ]
""".format(
            base_image=base_image,
            func_basename=os.path.basename(exec_script_filename),
            docker_working_dir=docker_working_dir,
        )

        return dockerfile

    def _package_and_upload(
        self,
        function: callable,
        args: List,
        kwargs: Dict,
        base_image: str,
        image_tag: str,
        docker_working_dir: str,
        result_filename: str,
    ) -> str:
        """Package a task using Docker and upload it to AWS ECR.

        Args:
            function: A callable Python function.
            args: Positional arguments consumed by the task.
            kwargs: Keyword arguments consumed by the task.
            base_image: Name of the base image on which to build the task image.
            image_tag: Tag used to identify the Docker image.
            docker_working_dir: Working directory inside the Docker container.
            result_filename: Name of the pickled result.

        Returns:
            image_uri: URI of the uploaded image.
        """

        func_filename = f"func-{image_tag}.pkl"

        with tempfile.NamedTemporaryFile(dir=self.cache_dir) as function_file:
            # Write serialized function to file
            pickle.dump((function, args, kwargs), function_file)
            function_file.flush()

            # Move pickled function to data store
            if self.data_store.startswith("s3://"):
                import boto3

                s3 = boto3.client("s3")
                res = s3.upload_file(
                    function_file.name, self.data_store[5:].split("/")[0], func_filename
                )

            else:
                shutil.copyfile(function_file.name, os.path.join(self.data_store, func_filename))

        with tempfile.NamedTemporaryFile(
            dir=self.cache_dir, mode="w"
        ) as exec_script_file, tempfile.NamedTemporaryFile(
            dir=self.cache_dir, mode="w"
        ) as dockerfile_file:
            # Write execution script to file
            exec_script = self._format_exec_script(
                func_filename,
                result_filename,
                docker_working_dir,
            )

            exec_script_file.write(exec_script)
            exec_script_file.flush()

            if self.data_store.startswith("/"):
                shutil.copyfile(
                    exec_script_file.name,
                    os.path.join(self.data_store, exec_script_file.name.split("/")[-1]),
                )

            # Write Dockerfile to file
            dockerfile = self._format_dockerfile(
                exec_script_file.name,
                docker_working_dir,
                base_image,
            )
            dockerfile_file.write(dockerfile)
            dockerfile_file.flush()

            # Build the Docker image
            docker_client = docker.from_env()

            image, build_log = docker_client.images.build(
                path=self.cache_dir, dockerfile=dockerfile_file.name, tag=image_tag
            )

        if "amazonaws.com" in self.registry:
            # Login to AWS ECR
            import boto3

            if self.registry_credentials_file:
                os.environ["AWS_SHARED_CREDENTIALS_FILE"] = self.registry_credentials_file

            sts = boto3.client("sts")
            identity = sts.get_caller_identity()

            app_log.debug(f"Identity: {str(identity)}")

            ecr = boto3.client("ecr")

            ecr_username = "AWS"
            ecr_credentials = ecr.get_authorization_token()["authorizationData"][0]

            ecr_password = (
                base64.b64decode(ecr_credentials["authorizationToken"])
                .replace(b"AWS:", b"")
                .decode("utf-8")
            )

            ecr_registry = ecr_credentials["proxyEndpoint"]
            image_uri = f"{ecr_registry.replace('https://', '')}/{self.image_repo}:{image_tag}"

            response = docker_client.login(
                username=ecr_username, password=ecr_password, registry=ecr_registry
            )

        elif "localhost" not in self.registry and self.registry_credentials_file:
            # Login using credentials file
            credentials = toml.load(self.registry_credentials_file)

            response = docker_client.login(
                username=credentials["username"],
                password=credentials["password"],
                registry=self.registry,
            )

            image_uri = f"{self.registry.replace('https://', '')}/{self.image_repo}:{image_tag}"

        else:
            # Image remains on the server for local use
            image_uri = f"{self.image_repo}:{image_tag}"

        # Tag the image
        image.tag(image_uri, tag=image_tag)

        # Push the image
        if "localhost" in self.registry:
            # If local we assume minikube is running
            proc = subprocess.run(["minikube", "image", "load", image_uri], check=True)

            if proc.returncode != 0:
                raise Exception(proc.stderr.decode("utf-8"))
        else:
            response = docker_client.images.push(image_uri, tag=image_tag)

        return image_uri

    # TODO: These exit codes should be mapped to enum statuses
    def get_status(self, api_client, name: str, namespace: Optional[str] = "default") -> int:
        """Query the status of a previously submitted EKS job.

        Args:
            name: Kubernetes job name.
            namespace: namespace of job job.

        Returns:
            exit_code: Exit code, if the task has completed, else -1.
        """

        # Create an instance of the API class
        api_instance = client.BatchV1Api(api_client=api_client)

        job = api_instance.read_namespaced_job_status(name, namespace)

        if job.status.succeeded is not None:
            if int(job.status.succeeded) > 0:
                return 1
            elif job.status.active is not None and int(job.status.active) > 0:
                return 0

            return -2

        return -3

    def _poll_task(self, api_client, name: str, namespace: Optional[str] = "default") -> None:
        """Poll a Kubernetes task until completion.

        Args:
            api_client: Kubernetes API client.
            name: Kubernetes job name.
            namespace: namespace of job.

        Returns:
            None
        """

        exit_code = self.get_status(api_client, name, namespace)

        while exit_code != 1:
            time.sleep(self.poll_freq)
            exit_code = self.get_status(api_client, name, namespace)

            if exit_code == 0:
                app_log.debug("Waiting for job completion")

            if exit_code == -1 or exit_code == -2:
                api_instance = client.BatchV1Api(api_client=api_client)
                job = api_instance.read_namespaced_job_status(name, namespace)

                app_log.debug("Error while polling job")
                app_log.debug(job)

    def _query_result(
        self,
        result_filename: str,
        image_tag: str,
    ) -> Tuple[Any, str, str]:
        """Query and retrieve a completed task's result.

        Args:
            result_filename: Name of the pickled result file.
            task_arn: ARN used to identify an ECS task.
            image_tag: Tag used to identify the Docker image.

        Returns:
            result: The task's result, as a Python object.
            logs: The stdout and stderr streams corresponding to the task.
            empty_string: A placeholder empty string.
        """

        if self.data_store.startswith("s3://"):
            import boto3

            s3 = boto3.client("s3")
            s3.download_file(
                self.data_store[5:].split("/")[0],
                result_filename,
                os.path.join(self.cache_dir, result_filename),
            )
        else:
            shutil.copyfile(
                os.path.join(self.data_store, result_filename),
                os.path.join(self.cache_dir, result_filename),
            )

        with open(os.path.join(self.cache_dir, result_filename), "rb") as f:
            result = pickle.load(f)
        os.remove(os.path.join(self.cache_dir, result_filename))

        return result
