# Common — Bedrock chatbot

A small Flask chatbot with a responsive web UI, multi-turn Claude Opus 4.8 conversations, browser-local history, and Docker packaging. The Python backend calls Amazon Bedrock through Boto3's Converse API. There is no simulated response mode.

Deployment target:

| Setting | Value |
| --- | --- |
| AWS Region | `us-east-1` (N. Virginia) |
| Bedrock inference profile | `us.anthropic.claude-opus-4-8` |
| ECR repository | `chatbot-baka` |
| EC2 Name tag | `silknet-deployment-baka` |
| Instance type | `t3.micro` (x86_64) |
| Ubuntu image | Latest standard Ubuntu Server 26.04 LTS, resolved from Canonical's public SSM parameter at launch |
| Root storage | 8 GB gp3, deleted on termination |
| Security group | Existing `default` group in the default VPC; rules are left unchanged |
| SSH key pair | `silknet-deployment-baka` |

Standard Ubuntu has no Ubuntu Pro license charge. Free Tier eligibility depends on your AWS account and its current allowances; EC2, EBS, public IPv4, ECR storage, and Bedrock usage can incur charges.

## 1. Run locally with venv

Requires Python 3.10+ (Docker uses Python 3.14).

```bash
bash scripts/setup.sh
source .venv/bin/activate
python app.py
```

Open <http://127.0.0.1:8000>. Configure `AWS_REGION` and `BEDROCK_MODEL_ID` in `.env` if needed. First check whether your existing AWS credentials work:

```bash
aws sts get-caller-identity --profile default --region us-east-1
```

If this returns the intended account and IAM identity, use that profile without running `aws login`. A profile that already contains access keys cannot also be configured for console login. To use your browser console session instead, create a separate named profile:

```bash
aws login --profile chatbot-baka
export AWS_PROFILE=chatbot-baka
aws sts get-caller-identity --region us-east-1
```

`aws login` requires AWS CLI 2.32+ and the `SignInLocalDevelopmentAccess` permission. The app includes Boto3's CRT extra to support these console login sessions. Use `aws sso login --profile YOUR_PROFILE` if your organization uses IAM Identity Center. Set `AWS_PROFILE` in `.env` or export it in your terminal for a named local profile, then restart the application. Your IAM username (`silknet7`) is not automatically an AWS CLI profile name. Signing into the browser console does not authenticate a local Python program. Keep your existing `default` credentials; changing login methods does not require deleting them.

If the browser returns **400 Bad Request**, stale browser sessions are one reported cause ([AWS CLI issue](https://github.com/aws/aws-cli/issues/10186)). Cancel the pending login with Ctrl+C. Open a new private/incognito window and sign into the AWS console there as the intended IAM user. Then start a new login:

```bash
aws login --profile chatbot-baka --region us-east-1 --remote
```

Open the newly generated URL in that same private window. Complete authorization and paste the resulting authorization code into the waiting terminal prompt. Keep the command running while completing this flow. Once login succeeds, verify with `aws sts get-caller-identity --profile chatbot-baka --region us-east-1`. AWS documents the [remote authorization flow](https://docs.aws.amazon.com/cli/latest/reference/login/). This is a troubleshooting step, not a confirmed diagnosis of every 400 response.

Your principal must have `bedrock:InvokeModel` access to the Opus inference profile and its destination foundation models. Complete any Anthropic first-use/model-access setup in Bedrock. Requests to the US profile can be routed within the US; this is not single-region inference.

## 2. Build and run the container

Start Docker Desktop or Rancher Desktop with its Docker engine enabled.

```bash
docker compose up --build -d
curl http://127.0.0.1:8000/health
docker compose logs -f
```

Compose alone does not inject AWS credentials. For local container inference using a readable AWS credentials/config directory:

```bash
docker compose -f compose.yaml -f compose.local-aws.yaml up --build -d
```

The mount is read-only and the container runs as UID 10001. If host permissions prevent it from reading `~/.aws`, or your AWS login uses cached credentials whose paths differ inside Docker, use temporary session credentials instead:

```bash
(
  eval "$(aws configure export-credentials --format env)"
  docker compose run --rm --service-ports \
    -e AWS_ACCESS_KEY_ID -e AWS_SECRET_ACCESS_KEY -e AWS_SESSION_TOKEN chatbot
)
```

Stop any running Compose service before using `compose run` on the same port. Temporary credentials expire; refresh them when needed. `.env*` files are ignored by Git and excluded from the Docker build. On EC2, the container obtains credentials directly from its IAM role.

Stop the local container with `docker compose down`.

## 3. Build for t3.micro and push to ECR

Requires working Docker, AWS CLI, and a locally authenticated AWS session with ECR create/push permissions. Activate `.venv` first.

```bash
python scripts/deploy.py push
```

This builds **linux/amd64**, including on Apple Silicon, creates or reuses `chatbot-baka`, authenticates with a temporary ECR token, and pushes an image with a UTC timestamp plus random suffix. The build omits provenance and SBOM attestations for this single-platform deployment. The script reads the configuration digest from BuildKit's `--metadata-file` output, checks it against the ECR manifest, and records the verified immutable digest in `deployment-state.json`. This avoids confusing Docker's index/manifest IDs with configuration digests. No credentials are added to the image. The ECR repository uses immutable tags when created by the script. If supplying `--tag`, choose a new tag for each build.

If Docker reports an immutable-tag error after uploading, the script checks whether the existing ECR image matches the local build. A matching image can be used directly; a different image is rejected. To recover the image from the earlier interrupted push without rebuilding or uploading again:

```bash
python scripts/deploy.py --profile chatbot-baka verify-image --tag 20261106-102105
```

This command reads Docker/ECR metadata and writes local state only. It handles supported OCI indexes, selecting a matching linux/amd64 image rather than its attestation. Continue to EC2 only after it prints `Image verified in ECR:`. If verification cannot match the local image ID to the registry metadata (for example, a local index against a registry that stored only its child manifest), run `push` again. A fresh build uses an explicit configuration digest and a fresh tag; Docker reuses available build cache and skips uploading layers already stored in ECR. Digest mismatch errors show the expected and observed public image hashes for diagnosis.

## 4. Create EC2 and deploy

The policies currently assigned to `silknet7` include EC2, ECR, IAM, and Bedrock access. Systems Manager needs separate operator permissions for the public Ubuntu image lookup, connection status, SSH, and UI tunnel. For account `881490130721`, the prepared `infra/operator-ssm-policy.json` grants image-parameter read access, regional connection-status reads, sessions to instances tagged `Project=chatbot-baka`, and management of this user's own sessions. Attach it as a named inline user policy using your authenticated profile:

```bash
aws iam put-user-policy --profile chatbot-baka \
  --user-name silknet7 \
  --policy-name ChatbotBakaSsmAccess \
  --policy-document file://infra/operator-ssm-policy.json
```

This command requires `iam:PutUserPolicy`, included in the user's inherited `IAMFullAccess` policy. The policy file is specific to the account and user above; review it before using a different identity. Session permissions follow the [AWS Session Manager examples](https://docs.aws.amazon.com/systems-manager/latest/userguide/getting-started-restrict-access-quickstart.html).

Then launch the instance:

```bash
python scripts/deploy.py launch
python scripts/deploy.py status
```

The deployment script:

1. Resolves and verifies Canonical's latest standard Ubuntu 26.04 amd64 image. It stops if that image needs more than the requested 8 GB.
2. Uses the existing default VPC, an available default subnet, and its unchanged default security group.
3. Generates an Ed25519 SSH private key at `keys/silknet-deployment-baka.pem` with mode `600` and imports its public key into EC2. Keep the private key and state file locally.
4. Creates an instance role with repository-scoped ECR pull permissions, Opus invocation permissions, and `AmazonSSMManagedInstanceCore` for Systems Manager.
5. Launches one `t3.micro` instance named `silknet-deployment-baka` with 8 GB gp3 storage and IMDSv2 required. Metadata hop limit 2 lets the Docker container use the role.
6. Installs Docker and the AWS CLI through cloud-init, pulls the exact ECR image digest, and starts `chatbot-baka` with restart enabled. Container logs rotate.

The deployment principal needs permission to create/import EC2 key pairs, launch/tag/describe EC2 resources, read the public SSM parameter, manage the named IAM role/profile/policy, and pass the instance role. It also needs Systems Manager session access for the helpers. The EC2 role and the deployment principal are separate.

Bootstrap takes a few minutes. Run `status` again after it completes. Existing project instances are reused by name rather than duplicated. A stopped existing instance is reported, not automatically restarted. The script does not replace or update an existing instance when a new image is pushed.

The default subnet needs working internet access and the default security group needs outbound HTTPS for image downloads, ECR, Bedrock, and Systems Manager. The script does not modify networking rules or create a default VPC if one is missing.

### If EC2 returns `VcpuLimitExceeded`

The account's regional Standard On-Demand vCPU quota needs room for the requested `t3.micro`, which uses 2 vCPUs. The verified image and prepared SSH/IAM resources can be reused. There is no need to push the image again.

Inspect running instances and their default vCPU counts with:

```bash
python scripts/deploy.py --profile chatbot-baka capacity --limit 16
```

`--limit` is the quota value reported in the launch error, not a fresh quota lookup. The report is read-only and counts running Standard On-Demand instances; it excludes Spot and separate instance-family quota buckets. Unused Capacity Reservations and concurrent account activity can also affect actual quota availability, so this inventory does not guarantee that a subsequent launch will succeed.

For the reported limit of 16, ask the account administrator to free at least 2 vCPUs on an unused instance whose owner approves the interruption, or request a higher **Running On-Demand Standard (A, C, D, H, I, M, R, T, Z) instances** quota in **us-east-1** (quota code `L-1216C47A`). A limit of 18 provides room for one additional `t3.micro` if the current usage is 16. The deployment script does not stop existing workloads or submit quota requests. See [EC2 On-Demand quotas](https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/ec2-on-demand-instances.html) and [requesting quota increases](https://docs.aws.amazon.com/servicequotas/latest/userguide/request-quota-increase.html).

After capacity is available, rerun `python scripts/deploy.py --profile chatbot-baka launch`.

## 5. Open the UI and SSH

Install the [AWS Session Manager plugin](https://docs.aws.amazon.com/systems-manager/latest/userguide/session-manager-working-with-install-plugin.html) on your computer. On macOS with Homebrew:

```bash
brew install --cask session-manager-plugin
session-manager-plugin --version
```

The instance state `running` confirms EC2 is up. It does not confirm that cloud-init finished or that the chatbot container is healthy. After a few minutes, check `status` again. Once it shows Systems Manager as `Online`, open a tunnel:

```bash
python scripts/deploy.py tunnel
```

Keep it running and open <http://127.0.0.1:8000>. In another terminal, `curl http://127.0.0.1:8000/health` should return `{"status":"ok"}`. Send a chat message to verify real Bedrock access. Systems Manager being online does not by itself confirm that the container is ready. Stop the local app/container first if it already uses port 8000.

For SSH using the generated key:

```bash
python scripts/deploy.py ssh
```

SSH runs through Systems Manager, so no inbound port 22 rule or public IP connection is needed. Accept the initial SSH host fingerprint only after verifying it in your AWS environment. The script preserves SSH host-key checking.

Inside EC2:

```bash
sudo docker ps
sudo docker logs chatbot-baka
curl http://127.0.0.1:8000/health
sudo tail -n 100 /var/log/cloud-init-output.log
```

With an unchanged default security group and a loopback-only app binding, the UI is accessed through the tunnel. There is no application login; keep it on this private access path. Public hosting would require a separate access and HTTPS setup.

## Validation

```bash
source .venv/bin/activate
python -m unittest discover -s tests -v
node --check static/app.js
docker compose config --quiet
```

Unit tests use Boto3's Stubber and never call a paid model. `/health` is a liveness check only; `Server ready` means Flask is reachable, not that model access has been verified. Send a message to verify real inference.

## AWS references

- [Opus 4.8 IDs, API compatibility, and availability](https://docs.aws.amazon.com/bedrock/latest/userguide/model-card-anthropic-claude-opus-4-8.html)
- [Boto3 Converse API](https://docs.aws.amazon.com/boto3/latest/reference/services/bedrock-runtime/client/converse.html)
- [Find current Ubuntu AMIs](https://ubuntu.com/aws/docs/aws-how-to/instances/find-ubuntu-images/)
- [Install Docker Engine on Ubuntu](https://docs.docker.com/engine/install/ubuntu/)
- [ECR authentication](https://docs.aws.amazon.com/AmazonECR/latest/userguide/registry_auth.html)
- [Start Systems Manager sessions](https://docs.aws.amazon.com/systems-manager/latest/userguide/session-manager-working-with-sessions-start.html)

## Cleanup

No deletion is automatic. When finished, terminate `silknet-deployment-baka` to stop instance charges (the root volume is deleted with it). Delete unwanted images/repository `chatbot-baka`, the EC2 key pair, and the project IAM role/profile separately. If you attached the operator policy above, remove the inline `ChatbotBakaSsmAccess` policy from `silknet7` when it is no longer needed. Keep the local private key until you have finished accessing the instance.
