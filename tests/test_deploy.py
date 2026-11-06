"""Validate launch parameters against AWS's SDK schemas without creating resources."""

import base64
import io
import json
import subprocess
import tempfile
import unittest
from argparse import Namespace
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

import boto3
from botocore.stub import ANY, Stubber

from scripts import deploy


class DeploymentTests(unittest.TestCase):
    def test_launch_preserves_default_group_and_requested_instance_size(self):
        self._launch_scenario()

    def test_quota_failure_keeps_verified_image_and_allows_a_fresh_retry(self):
        self._launch_scenario(quota_error=True)

    def _launch_scenario(self, quota_error=False):
        ec2 = boto3.client("ec2", region_name="us-east-1", aws_access_key_id="testing", aws_secret_access_key="testing")
        ssm = boto3.client("ssm", region_name="us-east-1", aws_access_key_id="testing", aws_secret_access_key="testing")
        with tempfile.TemporaryDirectory() as directory, patch.object(deploy, "STATE_FILE", Path(directory) / "state.json"):
            args = Namespace(profile=None, region="us-east-1", model="us.anthropic.claude-opus-4-8")
            task = deploy.Deployment(args)
            task.account = "123456789012"
            task.state = {"image_uri": "123456789012.dkr.ecr.us-east-1.amazonaws.com/chatbot-baka@sha256:" + "a" * 64}
            with Stubber(ec2) as ec2_stub, Stubber(ssm) as ssm_stub:
                ec2_stub.add_response("describe_instances", {"Reservations": []})
                ssm_stub.add_response("get_parameter", {"Parameter": {"Name": deploy.AMI_PARAMETER, "Type": "String", "Value": "ami-0123456789abcdef0"}}, {"Name": deploy.AMI_PARAMETER})
                ec2_stub.add_response("describe_images", {"Images": [{"ImageId": "ami-0123456789abcdef0", "Architecture": "x86_64", "Name": "ubuntu-26.04-test", "RootDeviceName": "/dev/sda1", "BlockDeviceMappings": [{"DeviceName": "/dev/sda1", "Ebs": {"VolumeSize": 8}}]}]}, {"ImageIds": ["ami-0123456789abcdef0"], "Owners": ["099720109477"]})
                ec2_stub.add_response("describe_vpcs", {"Vpcs": [{"VpcId": "vpc-test"}]})
                ec2_stub.add_response("describe_security_groups", {"SecurityGroups": [{"GroupId": "sg-default"}]})
                ec2_stub.add_response("describe_subnets", {"Subnets": [{"SubnetId": "subnet-test", "AvailabilityZone": "us-east-1a"}]})
                expected = {
                    "ImageId": "ami-0123456789abcdef0", "InstanceType": "t3.micro", "MinCount": 1, "MaxCount": 1,
                    "KeyName": "silknet-deployment-baka", "IamInstanceProfile": {"Name": "silknet-deployment-baka"},
                    "ClientToken": ANY, "UserData": ANY,
                    "NetworkInterfaces": [{"DeviceIndex": 0, "SubnetId": "subnet-test", "Groups": ["sg-default"], "AssociatePublicIpAddress": True}],
                    "BlockDeviceMappings": [{"DeviceName": "/dev/sda1", "Ebs": {"VolumeSize": 8, "VolumeType": "gp3", "DeleteOnTermination": True}}],
                    "MetadataOptions": {"HttpEndpoint": "enabled", "HttpTokens": "required", "HttpPutResponseHopLimit": 2},
                    "TagSpecifications": [{"ResourceType": r, "Tags": [{"Key": "Name", "Value": "silknet-deployment-baka"}, {"Key": "Project", "Value": "chatbot-baka"}]} for r in ("instance", "volume")],
                }
                if quota_error:
                    ec2_stub.add_client_error("run_instances", service_error_code="VcpuLimitExceeded", service_message="The current vCPU limit is 16", http_status_code=400, expected_params=expected)
                else:
                    ec2_stub.add_response("run_instances", {"Instances": [{"InstanceId": "i-0123456789abcdef0"}]}, expected)
                with patch.object(task, "identity"), patch.object(task, "ensure_key"), patch.object(task, "ensure_role"), patch.object(task, "client", side_effect=lambda name: {"ec2": ec2, "ssm": ssm}[name]):
                    if quota_error:
                        with self.assertRaisesRegex(RuntimeError, "t3.micro \\(2 vCPUs\\)"):
                            task.launch()
                        self.assertIn("image_uri", deploy.read_state())
                        self.assertNotIn("instance_id", deploy.read_state())
                        self.assertNotIn("launch_token", deploy.read_state())
                    else:
                        task.launch()
                        self.assertEqual(deploy.read_state()["instance_id"], "i-0123456789abcdef0")
                ec2_stub.assert_no_pending_responses()
                ssm_stub.assert_no_pending_responses()

    def test_capacity_reports_running_standard_instances_without_spot_or_gpu_usage(self):
        ec2 = boto3.client("ec2", region_name="us-east-1", aws_access_key_id="testing", aws_secret_access_key="testing")
        with tempfile.TemporaryDirectory() as directory, patch.object(deploy, "STATE_FILE", Path(directory) / "state.json"):
            task = deploy.Deployment(Namespace(profile=None, region="us-east-1", limit=16))
            with Stubber(ec2) as stub:
                stub.add_response("describe_instances", {"Reservations": [{"Instances": [
                    {"InstanceId": "i-standard", "InstanceType": "t3.micro", "Tags": [{"Key": "Name", "Value": "example-owner"}]},
                    {"InstanceId": "i-spot", "InstanceType": "t3.micro", "InstanceLifecycle": "spot"},
                    {"InstanceId": "i-gpu", "InstanceType": "g4dn.xlarge"},
                    {"InstanceId": "i-mac", "InstanceType": "mac2.metal"},
                ]}]}, {"Filters": [{"Name": "instance-state-name", "Values": ["running"]}]})
                stub.add_response("describe_instance_types", {"InstanceTypes": [{"InstanceType": "t3.micro", "VCpuInfo": {"DefaultVCpus": 2}}]}, {"InstanceTypes": ["t3.micro"]})
                with patch.object(task, "identity"), patch.object(task, "client", return_value=ec2), redirect_stdout(io.StringIO()):
                    report = task.capacity()
                self.assertEqual(report["running_standard_vcpus"], 2)
                self.assertEqual(report["reported_quota_vcpus"], 16)
                self.assertEqual([row["id"] for row in report["instances"]], ["i-standard"])
                self.assertFalse(deploy.STATE_FILE.exists())
                stub.assert_no_pending_responses()

    def test_instance_role_has_pull_access_and_opus_invocation_only(self):
        policy = deploy.runtime_policy("123456789012", "us-east-1", "us.anthropic.claude-opus-4-8")
        statements = policy["Statement"]
        self.assertEqual(statements[1]["Resource"], "arn:aws:ecr:us-east-1:123456789012:repository/chatbot-baka")
        actions = [action for statement in statements for action in ([statement["Action"]] if isinstance(statement["Action"], str) else statement["Action"])]
        self.assertIn("bedrock:InvokeModel", actions)
        self.assertFalse(any("PutImage" in action or "Create" in action or "Delete" in action for action in actions))
        self.assertNotIn("*", statements[2]["Resource"])


class ImageVerificationTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.state_file = Path(directory.name) / "state.json"
        state_patch = patch.object(deploy, "STATE_FILE", self.state_file)
        state_patch.start()
        self.addCleanup(state_patch.stop)
        args = Namespace(profile=None, region="us-east-1", model="us.anthropic.claude-opus-4-8", tag="existing-tag")
        self.task = deploy.Deployment(args)
        self.task.account = "123456789012"
        self.task.state = {"account": "123456789012", "region": "us-east-1"}
        self.ecr = boto3.client("ecr", region_name="us-east-1", aws_access_key_id="testing", aws_secret_access_key="testing")
        self.repository_uri = "123456789012.dkr.ecr.us-east-1.amazonaws.com/chatbot-baka"
        self.config_digest = "sha256:" + "a" * 64
        self.image_digest = "sha256:" + "b" * 64

    def add_repository(self, stub):
        stub.add_response("describe_repositories", {"repositories": [{"repositoryUri": self.repository_uri}]}, {"repositoryNames": ["chatbot-baka"]})

    def add_manifest(self, stub, image_id, manifest, digest):
        stub.add_response("batch_get_image", {"images": [{"imageId": {"imageDigest": digest}, "imageManifest": json.dumps(manifest)}]}, {"repositoryName": "chatbot-baka", "imageIds": [image_id]})

    def image_manifest(self, config_digest=None):
        return {"schemaVersion": 2, "mediaType": "application/vnd.oci.image.manifest.v1+json",
                "config": {"digest": config_digest or self.config_digest}, "layers": []}

    def run_verify(self, docker_id=None):
        inspected = subprocess.CompletedProcess([], 0, stdout=f"{docker_id or self.config_digest} linux amd64\n")
        with patch.object(self.task, "identity"), patch.object(self.task, "client", return_value=self.ecr), patch.object(deploy, "run", return_value=inspected), redirect_stdout(io.StringIO()):
            self.task.verify_image()

    def test_existing_matching_image_can_be_recorded_without_uploading(self):
        with Stubber(self.ecr) as stub:
            self.add_repository(stub)
            self.add_manifest(stub, {"imageTag": "existing-tag"}, self.image_manifest(), self.image_digest)
            self.run_verify()
            self.assertEqual(deploy.read_state()["image_uri"], f"{self.repository_uri}@{self.image_digest}")
            stub.assert_no_pending_responses()

    def test_oci_index_selects_matching_amd64_image_and_skips_attestation(self):
        index_digest = "sha256:" + "c" * 64
        index = {"schemaVersion": 2, "mediaType": "application/vnd.oci.image.index.v1+json", "manifests": [
            {"digest": "sha256:" + "d" * 64, "platform": {"os": "unknown", "architecture": "unknown"}},
            {"digest": self.image_digest, "platform": {"os": "linux", "architecture": "amd64"}},
        ]}
        with Stubber(self.ecr) as stub:
            self.add_repository(stub)
            self.add_manifest(stub, {"imageTag": "existing-tag"}, index, index_digest)
            self.add_manifest(stub, {"imageDigest": self.image_digest}, self.image_manifest(), self.image_digest)
            self.run_verify()
            self.assertEqual(deploy.read_state()["image_uri"], f"{self.repository_uri}@{self.image_digest}")
            stub.assert_no_pending_responses()

    def test_a_different_existing_image_is_never_selected_for_deployment(self):
        with Stubber(self.ecr) as stub:
            self.add_repository(stub)
            self.add_manifest(stub, {"imageTag": "existing-tag"}, self.image_manifest("sha256:" + "f" * 64), self.image_digest)
            with self.assertRaisesRegex(RuntimeError, "could not be matched") as error:
                self.run_verify()
            self.assertIn(self.config_digest, str(error.exception))
            self.assertIn(self.image_digest, str(error.exception))
            self.assertFalse(self.state_file.exists())
            stub.assert_no_pending_responses()

    def test_docker_manifest_identity_can_be_verified(self):
        with Stubber(self.ecr) as stub:
            self.add_repository(stub)
            self.add_manifest(stub, {"imageTag": "existing-tag"}, self.image_manifest(), self.image_digest)
            self.run_verify(docker_id=self.image_digest)
            self.assertEqual(deploy.read_state()["image_uri"], f"{self.repository_uri}@{self.image_digest}")

    def test_docker_index_identity_selects_its_amd64_manifest(self):
        index_digest = "sha256:" + "c" * 64
        index = {"schemaVersion": 2, "mediaType": "application/vnd.oci.image.index.v1+json", "manifests": [
            {"digest": self.image_digest, "platform": {"os": "linux", "architecture": "amd64"}},
        ]}
        with Stubber(self.ecr) as stub:
            self.add_repository(stub)
            self.add_manifest(stub, {"imageTag": "existing-tag"}, index, index_digest)
            self.add_manifest(stub, {"imageDigest": self.image_digest}, self.image_manifest(), self.image_digest)
            self.run_verify(docker_id=index_digest)
            self.assertEqual(deploy.read_state()["image_uri"], f"{self.repository_uri}@{self.image_digest}")

    def test_incomplete_push_does_not_create_deployment_state(self):
        with Stubber(self.ecr) as stub:
            self.add_repository(stub)
            stub.add_response("batch_get_image", {"images": [], "failures": [{"imageId": {"imageTag": "existing-tag"}, "failureCode": "ImageNotFound", "failureReason": "Not uploaded"}]}, {"repositoryName": "chatbot-baka", "imageIds": [{"imageTag": "existing-tag"}]})
            with self.assertRaisesRegex(RuntimeError, "no readable image"):
                self.run_verify()
            self.assertFalse(self.state_file.exists())

    def test_failed_push_is_recovered_only_after_matching_remote_content(self):
        def docker_command(command, **kwargs):
            if "--metadata-file" in command:
                Path(command[command.index("--metadata-file") + 1]).write_text(json.dumps({"containerimage.config.digest": self.config_digest}))
            if command[:3] == ["docker", "image", "inspect"]:
                return subprocess.CompletedProcess(command, 0, stdout=f"{self.config_digest} linux amd64\n")
            if "push" in command:
                raise subprocess.CalledProcessError(1, command)
            return subprocess.CompletedProcess(command, 0)

        with Stubber(self.ecr) as stub:
            self.add_repository(stub)
            stub.add_response("get_authorization_token", {"authorizationData": [{"authorizationToken": base64.b64encode(b"AWS:fake-test-token").decode(), "proxyEndpoint": "https://123456789012.dkr.ecr.us-east-1.amazonaws.com"}]}, {})
            self.add_manifest(stub, {"imageTag": "existing-tag"}, self.image_manifest(), self.image_digest)
            with patch.object(self.task, "identity"), patch.object(self.task, "client", return_value=self.ecr), patch.object(deploy, "run", side_effect=docker_command), redirect_stdout(io.StringIO()) as output:
                self.task.push()
            self.assertIn("Docker reported a push error", output.getvalue())
            self.assertIn("Image verified in ECR", output.getvalue())
            self.assertEqual(deploy.read_state()["image_uri"], f"{self.repository_uri}@{self.image_digest}")
            stub.assert_no_pending_responses()

    def test_new_push_uses_build_config_when_local_id_is_a_different_index(self):
        def docker_command(command, **kwargs):
            if "--metadata-file" in command:
                Path(command[command.index("--metadata-file") + 1]).write_text(json.dumps({"containerimage.config.digest": self.config_digest}))
            if command[:3] == ["docker", "image", "inspect"]:
                return subprocess.CompletedProcess(command, 0, stdout=f"sha256:{'c' * 64} linux amd64\n")
            return subprocess.CompletedProcess(command, 0)

        with Stubber(self.ecr) as stub:
            self.add_repository(stub)
            stub.add_response("get_authorization_token", {"authorizationData": [{"authorizationToken": base64.b64encode(b"AWS:fake-test-token").decode(), "proxyEndpoint": "https://123456789012.dkr.ecr.us-east-1.amazonaws.com"}]}, {})
            self.add_manifest(stub, {"imageTag": "existing-tag"}, self.image_manifest(), self.image_digest)
            with patch.object(self.task, "identity"), patch.object(self.task, "client", return_value=self.ecr), patch.object(deploy, "run", side_effect=docker_command), redirect_stdout(io.StringIO()):
                self.task.push()
            self.assertEqual(deploy.read_state()["image_uri"], f"{self.repository_uri}@{self.image_digest}")
            stub.assert_no_pending_responses()


if __name__ == "__main__":
    unittest.main()
