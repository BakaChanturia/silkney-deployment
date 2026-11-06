#!/usr/bin/env bash
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive
AWS_REGION=@@AWS_REGION@@
IMAGE_URI=@@IMAGE_URI@@
BEDROCK_MODEL_ID=@@MODEL_ID@@

apt-get update
apt-get install -y ca-certificates curl unzip
install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
chmod a+r /etc/apt/keyrings/docker.asc
cat > /etc/apt/sources.list.d/docker.sources <<EOF
Types: deb
URIs: https://download.docker.com/linux/ubuntu
Suites: $(. /etc/os-release && echo "${UBUNTU_CODENAME:-$VERSION_CODENAME}")
Components: stable
Architectures: $(dpkg --print-architecture)
Signed-By: /etc/apt/keyrings/docker.asc
EOF
apt-get update
apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
systemctl enable --now docker

task_workdir=$(mktemp -d)
trap 'rm -rf "$task_workdir"' EXIT
curl -fsSL https://awscli.amazonaws.com/awscli-exe-linux-x86_64.zip -o "$task_workdir/awscliv2.zip"
unzip -q "$task_workdir/awscliv2.zip" -d "$task_workdir"
"$task_workdir/aws/install" --update

# Ubuntu AWS images normally include the SSM agent as a snap.
if command -v snap >/dev/null && snap list amazon-ssm-agent >/dev/null 2>&1; then
  systemctl enable --now snap.amazon-ssm-agent.amazon-ssm-agent.service
else
  curl -fsSL "https://s3.${AWS_REGION}.amazonaws.com/amazon-ssm-${AWS_REGION}/latest/debian_amd64/amazon-ssm-agent.deb" -o "$task_workdir/ssm.deb"
  dpkg -i "$task_workdir/ssm.deb"
  systemctl enable --now amazon-ssm-agent
fi

# Credentials come from the instance role; no AWS keys are stored in this script.
mkdir -p "$task_workdir/docker"
task_registry=${IMAGE_URI%%/*}
aws ecr get-login-password --region "$AWS_REGION" |
  docker --config "$task_workdir/docker" login --username AWS --password-stdin "$task_registry"
docker --config "$task_workdir/docker" pull "$IMAGE_URI"
docker run -d \
  --name chatbot-baka \
  --restart unless-stopped \
  --init \
  --publish 127.0.0.1:8000:8000 \
  --env "AWS_REGION=$AWS_REGION" \
  --env "BEDROCK_MODEL_ID=$BEDROCK_MODEL_ID" \
  --env BEDROCK_MAX_TOKENS=2048 \
  --read-only \
  --tmpfs /tmp:rw,noexec,nosuid,size=32m,mode=1777 \
  --cap-drop ALL \
  --security-opt no-new-privileges:true \
  --log-opt max-size=10m \
  --log-opt max-file=3 \
  "$IMAGE_URI"
