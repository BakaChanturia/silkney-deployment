#!/usr/bin/env python3
"""Build/push to ECR, then launch the requested EC2 instance using its IAM role."""

import argparse
import base64
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
STATE_FILE = ROOT / "deployment-state.json"
INSTANCE_NAME = "silknet-deployment-baka"
REPOSITORY = "chatbot-baka"
ROLE_NAME = "silknet-deployment-baka"
KEY_NAME = "silknet-deployment-baka"
KEY_FILE = ROOT / "keys" / f"{KEY_NAME}.pem"
AMI_PARAMETER = "/aws/service/canonical/ubuntu/server/26.04/stable/current/amd64/hvm/ebs-gp3/ami-id"


def read_state():
    return json.loads(STATE_FILE.read_text()) if STATE_FILE.exists() else {}


def write_state(state):
    # Store identifiers only. Private keys and AWS credentials never go in state.
    temporary = STATE_FILE.with_suffix(".tmp")
    temporary.write_text(json.dumps(state, indent=2) + "\n")
    temporary.chmod(0o600)
    temporary.replace(STATE_FILE)


def run(command, **kwargs):
    return subprocess.run(command, check=True, cwd=ROOT, **kwargs)


def runtime_policy(account, region, profile_id):
    model = "anthropic.claude-opus-4-8"
    return {
        "Version": "2012-10-17",
        "Statement": [
            {"Effect": "Allow", "Action": "ecr:GetAuthorizationToken", "Resource": "*"},
            {
                "Effect": "Allow",
                "Action": ["ecr:BatchCheckLayerAvailability", "ecr:GetDownloadUrlForLayer", "ecr:BatchGetImage"],
                "Resource": f"arn:aws:ecr:{region}:{account}:repository/{REPOSITORY}",
            },
            {
                "Effect": "Allow",
                "Action": "bedrock:InvokeModel",
                "Resource": [
                    f"arn:aws:bedrock:{region}:{account}:inference-profile/{profile_id}",
                    *[f"arn:aws:bedrock:{r}::foundation-model/{model}" for r in ("us-east-1", "us-east-2", "us-west-2")],
                ],
            },
        ],
    }


class Deployment:
    def __init__(self, args):
        self.args = args
        self.session = boto3.Session(profile_name=args.profile, region_name=args.region)
        self.state = read_state()
        self.account = None

    def client(self, name):
        return self.session.client(name, config=Config(
            connect_timeout=5, read_timeout=15,
            retries={"mode": "standard", "total_max_attempts": 2},
        ))

    def identity(self):
        identity = self.client("sts").get_caller_identity()
        self.account = identity["Account"]
        for name, value in (("account", self.account), ("region", self.args.region)):
            if self.state.get(name) and self.state[name] != value:
                raise RuntimeError(f"Saved deployment uses a different {name}. Use its original AWS profile/region.")
        self.state.update(account=self.account, region=self.args.region)
        print(f"AWS identity: {identity['Arn']}\nRegion: {self.args.region}", flush=True)

    def push(self):
        # Verify the AWS identity and Docker before spending time on a build.
        # These preflight checks do not create AWS resources.
        self.identity()
        run(["docker", "info"], stdout=subprocess.DEVNULL, timeout=15)
        tag = self.args.tag or f"{datetime.now(timezone.utc):%Y%m%d-%H%M%S}-{uuid.uuid4().hex[:8]}"
        local_image = f"{REPOSITORY}:{tag}"
        with tempfile.TemporaryDirectory(prefix="chatbot-build-") as build_directory:
            metadata_path = Path(build_directory) / "metadata.json"
            run(["docker", "buildx", "build", "--platform", "linux/amd64", "--provenance=false", "--sbom=false", "--metadata-file", str(metadata_path), "--load", "-t", local_image, "."])
            try:
                metadata = json.loads(metadata_path.read_text())
            except (OSError, ValueError) as exc:
                raise RuntimeError("Could not read BuildKit's image metadata. No image was pushed.") from exc
            local_digest = metadata.get("containerimage.config.digest") if isinstance(metadata, dict) else None
            if not isinstance(local_digest, str) or not re.fullmatch(r"sha256:[0-9a-f]{64}", local_digest):
                raise RuntimeError("BuildKit did not return an image configuration digest. No image was pushed.")
        # Inspect validates the loaded platform. Verify registry content against
        # BuildKit's explicit config digest, never an ambiguous index/image ID.
        self.local_image_identity(local_image)
        print(f"Built image configuration: {local_digest}", flush=True)
        ecr = self.client("ecr")
        try:
            repository = ecr.describe_repositories(repositoryNames=[REPOSITORY])["repositories"][0]
        except ecr.exceptions.RepositoryNotFoundException:
            repository = ecr.create_repository(
                repositoryName=REPOSITORY,
                imageTagMutability="IMMUTABLE",
                tags=[{"Key": "Project", "Value": REPOSITORY}],
            )["repository"]
        image_tag = f"{repository['repositoryUri']}:{tag}"
        registry = repository["repositoryUri"].split("/")[0]
        credentials = ecr.get_authorization_token()["authorizationData"][0]
        username, password = base64.b64decode(credentials["authorizationToken"]).decode().split(":", 1)
        # Temporary Docker config prevents saving an ECR password in ~/.docker.
        with tempfile.TemporaryDirectory(prefix="chatbot-ecr-") as docker_config:
            run(["docker", "--config", docker_config, "login", "--username", username,
                 "--password-stdin", registry], input=password, text=True, capture_output=True)
            run(["docker", "tag", local_image, image_tag])
            try:
                run(["docker", "--config", docker_config, "push", image_tag])
            except subprocess.CalledProcessError:
                # Some clients report an immutable-tag error after ECR has stored
                # the image. Only accept it if the stored content matches exactly.
                print("Docker reported a push error. Checking the image already stored in ECR...", flush=True)
        digest = self.remote_image_digest(ecr, tag, local_digest)
        self.record_image(repository["repositoryUri"], tag, digest)

    @staticmethod
    def local_image_identity(local_image):
        result = run(["docker", "image", "inspect", "--format", "{{.Id}} {{.Os}} {{.Architecture}}", local_image], capture_output=True, text=True)
        fields = result.stdout.strip().split()
        if len(fields) != 3 or fields[1:] != ["linux", "amd64"]:
            raise RuntimeError("The local image must be built for linux/amd64 before it can be deployed to t3.micro.")
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", fields[0]):
            raise RuntimeError("Docker did not return a valid local image digest.")
        return fields[0]

    @staticmethod
    def remote_image_digest(ecr, tag, local_digest):
        def manifest_for(image_id):
            response = ecr.batch_get_image(repositoryName=REPOSITORY, imageIds=[image_id])
            images = response.get("images", [])
            if not images:
                raise RuntimeError(f"ECR has no readable image for {image_id}. No deployment state was saved; retry push with a new tag.")
            image = images[0]
            try:
                manifest = json.loads(image["imageManifest"])
                digest = image["imageId"]["imageDigest"]
            except (KeyError, ValueError, TypeError) as exc:
                raise RuntimeError("ECR returned an invalid image manifest. No deployment state was saved.") from exc
            if not isinstance(manifest, dict) or not re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
                raise RuntimeError("ECR returned an invalid image manifest or digest.")
            return manifest, digest

        manifest, digest = manifest_for({"imageTag": tag})
        observed = [f"manifest={digest}"]
        if manifest.get("config", {}).get("digest"):
            observed.append(f"config={manifest['config']['digest']}")
        if manifest.get("config", {}).get("digest") == local_digest or (manifest.get("config") and digest == local_digest):
            return digest
        # Existing builds may include an OCI index and an attestation. Select the
        # linux/amd64 image and pin its digest, excluding any attestation manifests.
        for descriptor in manifest.get("manifests", []):
            platform = descriptor.get("platform", {})
            if platform.get("os") == "linux" and platform.get("architecture") == "amd64":
                child, child_digest = manifest_for({"imageDigest": descriptor["digest"]})
                observed.append(f"linux/amd64 manifest={child_digest}, config={child.get('config', {}).get('digest', 'missing')}")
                # Image IDs can refer to the config, manifest, or index depending
                # on Docker's image store. Each match identifies the same content.
                if digest == local_digest or child_digest == local_digest or child.get("config", {}).get("digest") == local_digest:
                    return child_digest
        raise RuntimeError(
            f"ECR tag {tag!r} could not be matched to the local image. "
            f"Expected digest: {local_digest}. ECR digests: {'; '.join(observed)}. "
            "No image was selected for deployment. Run push again to use a fresh tag and BuildKit's configuration digest."
        )

    def record_image(self, repository_uri, tag, digest):
        self.state.update(image_uri=f"{repository_uri}@{digest}", image_tag=f"{repository_uri}:{tag}")
        write_state(self.state)
        print(f"Image verified in ECR: {self.state['image_uri']}\nNext: python scripts/deploy.py launch")

    def verify_image(self):
        """Recover an uploaded image without rebuilding or changing an ECR tag."""
        self.identity()
        local_digest = self.local_image_identity(f"{REPOSITORY}:{self.args.tag}")
        ecr = self.client("ecr")
        repository = ecr.describe_repositories(repositoryNames=[REPOSITORY])["repositories"][0]
        digest = self.remote_image_digest(ecr, self.args.tag, local_digest)
        self.record_image(repository["repositoryUri"], self.args.tag, digest)

    def ensure_key(self, ec2):
        try:
            remote = ec2.describe_key_pairs(KeyNames=[KEY_NAME], IncludePublicKey=True)["KeyPairs"][0]
        except ClientError as exc:
            if exc.response["Error"]["Code"] != "InvalidKeyPair.NotFound":
                raise
            remote = None
        if not KEY_FILE.exists():
            if remote:
                raise RuntimeError(f"AWS key {KEY_NAME} already exists, but {KEY_FILE} is missing. Restore the matching key before continuing.")
            KEY_FILE.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", KEY_NAME, "-f", str(KEY_FILE)])
        KEY_FILE.chmod(0o600)
        public_key = run(["ssh-keygen", "-y", "-f", str(KEY_FILE)], capture_output=True, text=True).stdout.strip()
        if remote:
            if remote.get("PublicKey", "").split()[:2] != public_key.split()[:2]:
                raise RuntimeError("The existing AWS SSH key does not match the local private key.")
        else:
            ec2.import_key_pair(KeyName=KEY_NAME, PublicKeyMaterial=public_key.encode(),
                                TagSpecifications=[{"ResourceType": "key-pair", "Tags": [{"Key": "Project", "Value": REPOSITORY}]}])

    def ensure_role(self):
        iam = self.client("iam")
        trust = {"Version": "2012-10-17", "Statement": [{"Effect": "Allow", "Principal": {"Service": "ec2.amazonaws.com"}, "Action": "sts:AssumeRole"}]}
        try:
            role = iam.get_role(RoleName=ROLE_NAME)["Role"]
            if {t["Key"]: t["Value"] for t in role.get("Tags", [])}.get("Project") != REPOSITORY:
                raise RuntimeError(f"IAM role {ROLE_NAME} already exists without this project's tag; refusing to modify it.")
        except iam.exceptions.NoSuchEntityException:
            iam.create_role(RoleName=ROLE_NAME, AssumeRolePolicyDocument=json.dumps(trust),
                            Tags=[{"Key": "Project", "Value": REPOSITORY}])
        iam.attach_role_policy(RoleName=ROLE_NAME, PolicyArn="arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore")
        iam.put_role_policy(RoleName=ROLE_NAME, PolicyName="ChatbotBedrockAndEcr", PolicyDocument=json.dumps(
            runtime_policy(self.account, self.args.region, self.args.model)))
        try:
            profile = iam.get_instance_profile(InstanceProfileName=ROLE_NAME)["InstanceProfile"]
        except iam.exceptions.NoSuchEntityException:
            profile = iam.create_instance_profile(InstanceProfileName=ROLE_NAME,
                       Tags=[{"Key": "Project", "Value": REPOSITORY}])["InstanceProfile"]
        if not profile.get("Roles"):
            iam.add_role_to_instance_profile(InstanceProfileName=ROLE_NAME, RoleName=ROLE_NAME)
        elif [r["RoleName"] for r in profile["Roles"]] != [ROLE_NAME]:
            raise RuntimeError("The instance profile already contains an unexpected role.")

    def capacity(self):
        """Report running Standard On-Demand vCPUs without changing instances."""
        if self.args.limit is not None and self.args.limit <= 0:
            raise RuntimeError("The reported vCPU limit must be positive.")
        self.identity()
        ec2 = self.client("ec2")
        instances = []
        pages = ec2.get_paginator("describe_instances").paginate(Filters=[
            {"Name": "instance-state-name", "Values": ["running"]},
        ])
        for page in pages:
            for reservation in page["Reservations"]:
                for instance in reservation["Instances"]:
                    if instance.get("InstanceLifecycle") in {"spot", "capacity-block"}:
                        continue
                    family = instance["InstanceType"].split(".")[0]
                    # Standard families use numbered names. This excludes separate
                    # buckets such as mac, hpc, inf, trn, dl, GPU, and high-memory U.
                    if re.match(r"^(?:[acdhimrtz]\d|i[ms]\d)", family):
                        instances.append(instance)
        types = sorted({instance["InstanceType"] for instance in instances})
        vcpus = {}
        for offset in range(0, len(types), 100):
            details = ec2.describe_instance_types(InstanceTypes=types[offset:offset + 100])["InstanceTypes"]
            vcpus.update({detail["InstanceType"]: detail["VCpuInfo"]["DefaultVCpus"] for detail in details})
        rows = []
        for instance in instances:
            tags = {tag["Key"]: tag["Value"] for tag in instance.get("Tags", [])}
            rows.append({"id": instance["InstanceId"], "name": tags.get("Name", "(unnamed)"),
                         "type": instance["InstanceType"], "vcpus": vcpus[instance["InstanceType"]]})
        report = {"region": self.args.region, "running_standard_vcpus": sum(row["vcpus"] for row in rows),
                  "reported_quota_vcpus": self.args.limit, "requested_t3_micro_vcpus": 2,
                  "instances": sorted(rows, key=lambda row: (row["name"], row["id"]))}
        print(json.dumps(report, indent=2))
        return report

    def launch(self):
        if self.args.region != "us-east-1" or self.args.model != "us.anthropic.claude-opus-4-8":
            raise RuntimeError("This EC2 policy is scoped to Opus 4.8 from us-east-1. Update the IAM policy before changing these settings.")
        self.identity()
        ec2 = self.client("ec2")
        if not self.state.get("image_uri"):
            raise RuntimeError("Push the Docker image first: python scripts/deploy.py push")
        existing = ec2.describe_instances(Filters=[
            {"Name": "tag:Name", "Values": [INSTANCE_NAME]},
            {"Name": "instance-state-name", "Values": ["pending", "running", "stopping", "stopped"]},
        ])["Reservations"]
        instances = [i for reservation in existing for i in reservation["Instances"]]
        if instances:
            if len(instances) != 1:
                raise RuntimeError("Multiple instances have the requested name. Resolve the duplicate before continuing.")
            instance = instances[0]
            if {t["Key"]: t["Value"] for t in instance.get("Tags", [])}.get("Project") != REPOSITORY:
                raise RuntimeError("An instance with this name exists outside this project; refusing to reuse it.")
            self.state["instance_id"] = instance["InstanceId"]
            write_state(self.state)
            print(f"Instance already exists: {instance['InstanceId']}. Use the status command; no duplicate was created.")
            return
        # Read all launch prerequisites before creating a key or IAM resources.
        image_id = self.client("ssm").get_parameter(Name=AMI_PARAMETER)["Parameter"]["Value"]
        images = ec2.describe_images(ImageIds=[image_id], Owners=["099720109477"])["Images"]
        if not images or images[0].get("Architecture") != "x86_64":
            raise RuntimeError("Could not verify the Canonical x86_64 Ubuntu image.")
        image = images[0]
        root_name = image["RootDeviceName"]
        minimum_size = next(block["Ebs"]["VolumeSize"] for block in image["BlockDeviceMappings"] if block["DeviceName"] == root_name)
        if minimum_size > 8:
            raise RuntimeError(f"The latest Ubuntu AMI requires {minimum_size} GB. The requested disk size is 8 GB; choose another standard AMI before launch.")
        vpcs = ec2.describe_vpcs(Filters=[{"Name": "is-default", "Values": ["true"]}])["Vpcs"]
        if not vpcs:
            raise RuntimeError("This region has no default VPC. Create/select a VPC before deployment.")
        vpc_id = vpcs[0]["VpcId"]
        groups = ec2.describe_security_groups(Filters=[{"Name": "vpc-id", "Values": [vpc_id]}, {"Name": "group-name", "Values": ["default"]}])["SecurityGroups"]
        subnets = ec2.describe_subnets(Filters=[{"Name": "vpc-id", "Values": [vpc_id]}, {"Name": "default-for-az", "Values": ["true"]}, {"Name": "state", "Values": ["available"]}])["Subnets"]
        if not groups or not subnets:
            raise RuntimeError("A default security group and available default subnet are required.")
        subnet = sorted(subnets, key=lambda item: item["AvailabilityZone"])[0]
        self.ensure_key(ec2)
        self.ensure_role()
        user_data = (ROOT / "infra" / "user-data.sh.tpl").read_text()
        for token, value in (("AWS_REGION", self.args.region), ("IMAGE_URI", self.state["image_uri"]), ("MODEL_ID", self.args.model)):
            user_data = user_data.replace(f"@@{token}@@", shlex.quote(value))
        self.state.setdefault("launch_token", str(uuid.uuid4()))
        self.state.update(ami_id=image_id, ami_name=image["Name"], security_group_id=groups[0]["GroupId"])
        write_state(self.state)
        tags = [{"Key": "Name", "Value": INSTANCE_NAME}, {"Key": "Project", "Value": REPOSITORY}]
        parameters = dict(
            ImageId=image_id, InstanceType="t3.micro", MinCount=1, MaxCount=1,
            KeyName=KEY_NAME, IamInstanceProfile={"Name": ROLE_NAME},
            ClientToken=self.state["launch_token"], UserData=user_data,
            NetworkInterfaces=[{"DeviceIndex": 0, "SubnetId": subnet["SubnetId"], "Groups": [groups[0]["GroupId"]], "AssociatePublicIpAddress": True}],
            BlockDeviceMappings=[{"DeviceName": root_name, "Ebs": {"VolumeSize": 8, "VolumeType": "gp3", "DeleteOnTermination": True}}],
            # Bridge-network containers need hop limit 2 to obtain role credentials.
            MetadataOptions={"HttpEndpoint": "enabled", "HttpTokens": "required", "HttpPutResponseHopLimit": 2},
            TagSpecifications=[{"ResourceType": resource, "Tags": tags} for resource in ("instance", "volume")],
        )
        for attempt in range(12):
            try:
                instance = ec2.run_instances(**parameters)["Instances"][0]
                break
            except ClientError as exc:
                error = exc.response["Error"]
                if error["Code"] == "VcpuLimitExceeded":
                    # EC2 rejected this request without creating an instance.
                    # A later attempt can resolve the latest AMI with a new token.
                    self.state.pop("launch_token", None)
                    write_state(self.state)
                    raise RuntimeError(
                        f"The EC2 Standard On-Demand vCPU quota in {self.args.region} has no room for this t3.micro (2 vCPUs). "
                        "The verified ECR image, SSH key, and IAM setup are ready to reuse. "
                        "Inspect running instances with the capacity command, then ask the account administrator to free capacity "
                        "or increase quota L-1216C47A. Retry launch after capacity is available. "
                        f"AWS detail: {error.get('Message', '')}"
                    ) from exc
                if error["Code"] in {"InvalidParameterValue", "InvalidIamInstanceProfile.NotFound"} and "instance profile" in error.get("Message", "").lower() and attempt < 11:
                    print("Waiting for the new IAM instance profile to propagate...", flush=True)
                    time.sleep(10)
                    continue
                raise
        self.state["instance_id"] = instance["InstanceId"]
        write_state(self.state)
        print(f"Launched {INSTANCE_NAME}: {instance['InstanceId']}\nUbuntu: {image['Name']}\nThe default security group is unchanged. Bootstrap takes a few minutes.\nNext: python scripts/deploy.py status")

    def instance_id(self):
        instance_id = self.state.get("instance_id")
        if not instance_id:
            raise RuntimeError("No instance is recorded. Run the launch command first.")
        return instance_id

    def status(self):
        self.identity()
        instance_id = self.instance_id()
        instance = self.client("ec2").describe_instances(InstanceIds=[instance_id])["Reservations"][0]["Instances"][0]
        info = self.client("ssm").describe_instance_information(Filters=[{"Key": "InstanceIds", "Values": [instance_id]}])["InstanceInformationList"]
        print(json.dumps({"instance_id": instance_id, "state": instance["State"]["Name"], "public_ip": instance.get("PublicIpAddress"),
                          "ssm": info[0]["PingStatus"] if info else "Not registered yet", "image": self.state.get("image_uri")}, indent=2))

    def access(self, ssh=False):
        instance_id = self.instance_id()
        if not shutil.which("session-manager-plugin"):
            hint = "Run: brew install --cask session-manager-plugin" if sys.platform == "darwin" else "Install the AWS Session Manager plugin for your operating system."
            raise RuntimeError(f"The AWS Session Manager plugin is missing from PATH. {hint}")
        region = self.state.get("region", self.args.region)
        aws = ["aws", "--region", region]
        if self.args.profile:
            aws += ["--profile", self.args.profile]
        if ssh:
            if not KEY_FILE.exists():
                raise RuntimeError(f"SSH private key is missing: {KEY_FILE}")
            KEY_FILE.chmod(0o600)
            proxy = shlex.join(aws + ["ssm", "start-session", "--target", instance_id,
                      "--document-name", "AWS-StartSSHSession", "--parameters", "portNumber=22"])
            command = ["ssh", "-i", str(KEY_FILE), "-o", "IdentitiesOnly=yes", "-o", f"ProxyCommand={proxy}", f"ubuntu@{instance_id}"]
        else:
            print("Open http://127.0.0.1:8000 while this session stays open.", flush=True)
            command = aws + ["ssm", "start-session", "--target", instance_id,
                       "--document-name", "AWS-StartPortForwardingSession",
                       "--parameters", json.dumps({"portNumber": ["8000"], "localPortNumber": ["8000"]})]
        run(command)


def main():
    load_dotenv(ROOT / ".env")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", default=os.getenv("AWS_PROFILE"))
    parser.add_argument("--region", default=os.getenv("AWS_REGION", "us-east-1"))
    parser.add_argument("--model", default=os.getenv("BEDROCK_MODEL_ID", "us.anthropic.claude-opus-4-8"))
    subparsers = parser.add_subparsers(dest="command", required=True)
    push = subparsers.add_parser("push", help="Build amd64 image and push it to chatbot-baka in ECR")
    push.add_argument("--tag", help="Optional unique tag; defaults to a UTC timestamp plus random suffix")
    verify = subparsers.add_parser("verify-image", help="Verify an uploaded image against its local build and save its deployment digest")
    verify.add_argument("--tag", required=True, help="Tag from the completed or interrupted push")
    capacity = subparsers.add_parser("capacity", help="List running Standard On-Demand instances and their default vCPU usage")
    capacity.add_argument("--limit", type=int, help="Optional quota value reported by EC2, for comparison")
    subparsers.add_parser("launch", help="Create IAM role, SSH key, and t3.micro EC2 instance")
    subparsers.add_parser("status", help="Read EC2 and Systems Manager status")
    subparsers.add_parser("ssh", help="SSH to EC2 through Systems Manager using the generated key")
    subparsers.add_parser("tunnel", help="Open a local port-forward to the chatbot UI")
    args = parser.parse_args()
    try:
        deployment = Deployment(args)
        if args.command in {"ssh", "tunnel"}:
            deployment.access(ssh=args.command == "ssh")
        else:
            getattr(deployment, args.command.replace("-", "_"))()
    except (BotoCoreError, ClientError, RuntimeError, OSError) as exc:
        print(f"Deployment stopped: {exc}", file=sys.stderr)
        return 1
    except subprocess.CalledProcessError as exc:
        # Never print stdin or captured login output, which may contain credentials.
        print(f"Command failed with exit code {exc.returncode}. Check the command output above.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
