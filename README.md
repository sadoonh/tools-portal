# Deploy the same app in another AWS account

Use the same architecture and application code. Only the Terraform settings and
image repository addresses change. No deployment wrapper, extra workspaces,
policy questionnaires or new architecture are needed.

## Git configuration layout

Commit the application, Terraform definitions, dependency pins and
`infra/company.tfvars.example`. Keep each account's actual settings in
`infra/terraform.tfvars`, which is ignored by Git. State, plans, `.env` files and
local AWS/Docker credentials are ignored too. AWS credentials belong in your
normal AWS CLI profile, not the example or application code.

```text
infra/company.tfvars.example  # committed: placeholders and defaults
infra/terraform.tfvars        # local only: account settings and image URIs
```

## 1. Configure the account

Start with a fresh clone on the company PC. Use your normal company AWS login
or profile. Install Python 3.12+, AWS CLI v2, Terraform 1.16.5 and Docker.
The commands below use Bash (Linux, macOS or WSL).

```sh
# Optional if you use a named profile:
export AWS_PROFILE=company-deployer

cp infra/company.tfvars.example infra/terraform.tfvars
```

Edit `infra/terraform.tfvars`:

- `aws_account_id`, `aws_region`, `project_name`, `owner`
- `vpc_cidr`, `availability_zones`
- `marimohub_allowed_email_domains`, optionally `portal_title`

Keep `auth_enabled=true` and `marimohub_enabled=true`. Initially leave the service
and link switches false and image URIs empty, as in the example. Use fresh local
Terraform state; do not copy state or local deployment settings from the old
account. Terraform derives the new URLs, Cognito clients, roles and resource
names from these inputs. If Terraform rejects an AZ, select another standard AZ
in your Region; AZ mappings differ between accounts.

## 2. Create infrastructure first

```sh
terraform -chdir=infra init
terraform -chdir=infra plan -out=foundation.tfplan
terraform -chdir=infra apply foundation.tfplan
```

This creates the network, ECR repositories, Cognito, storage and CloudFront
resources without starting application tasks. This first apply is necessary
because the repositories must exist before images can be pushed.

## 3. Build and push the three images

Get the repository addresses and use a new release tag:

```sh
export AWS_REGION=$(terraform -chdir=infra output -raw aws_region)
export AWS_DEFAULT_REGION="$AWS_REGION"
PORTAL_REPO=$(terraform -chdir=infra output -raw ecr_repository_url)
HUB_REPO="${PORTAL_REPO}-hub"
KERNEL_REPO="${PORTAL_REPO}-kernel"
REGISTRY="${PORTAL_REPO%%/*}"
RELEASE="release-$(date -u +%Y%m%dT%H%M%SZ)"

aws ecr get-login-password --region "$AWS_REGION" \
  | docker login --username AWS --password-stdin "$REGISTRY"
docker build --platform linux/amd64 -t "$PORTAL_REPO:$RELEASE" .
docker build --platform linux/amd64 -t "$HUB_REPO:$RELEASE" marimohub/hub
docker build --platform linux/amd64 -t "$KERNEL_REPO:$RELEASE" marimohub/kernel
docker push "$PORTAL_REPO:$RELEASE"
docker push "$HUB_REPO:$RELEASE"
docker push "$KERNEL_REPO:$RELEASE"
```

If Docker is unavailable, use the existing Linux Dockerless builder and Crane
0.22.1 instead (no AWS builder or deployment framework):

```sh
python3 marimohub/build_images.py portal "/tmp/portal-$RELEASE"
python3 marimohub/build_images.py hub-security "/tmp/hub-$RELEASE"
python3 marimohub/build_images.py kernel "/tmp/kernel-$RELEASE"
aws ecr get-login-password --region "$AWS_REGION" \
  | crane auth login --username AWS --password-stdin "$REGISTRY"
crane push "/tmp/portal-$RELEASE/image.tar" "$PORTAL_REPO:$RELEASE"
crane push "/tmp/hub-$RELEASE/image.tar" "$HUB_REPO:$RELEASE"
crane push "/tmp/kernel-$RELEASE/image.tar" "$KERNEL_REPO:$RELEASE"
```

Both build paths use pinned Linux/amd64 images, the non-root user and the same
runtime configuration. The portal/kernel use Alpine; the hub preserves the
pinned official Marimohub 0.4.16 application. All builds keep account settings
and secrets out of images. Existing ECR scan-on-push remains enabled; review
scan results before running a new image, as with the original deployment.

Resolve the immutable image URIs:

```sh
for REPO in "$PORTAL_REPO" "$HUB_REPO" "$KERNEL_REPO"; do
  DIGEST=$(aws ecr describe-images --region "$AWS_REGION" \
    --repository-name "${REPO#*/}" --image-ids "imageTag=$RELEASE" \
    --query 'imageDetails[0].imageDigest' --output text)
  printf '%s@%s\n' "$REPO" "$DIGEST"
done
```

## 4. Enable the app

Paste the three printed URIs into the corresponding values in
`infra/terraform.tfvars`, then enable the existing switches:

```hcl
image_uri                 = "NEW_PORTAL_REPOSITORY@sha256:DIGEST"
marimohub_server_image_uri = "NEW_HUB_REPOSITORY@sha256:DIGEST"
marimohub_kernel_image_uri = "NEW_KERNEL_REPOSITORY@sha256:DIGEST"

enable_service               = true
marimohub_service_enabled     = true
marimohub_portal_link_enabled = true
marimohub_proxy_ack           = true # Same trusted-notebook-author model as the working app.
```

```sh
terraform -chdir=infra plan -out=services.tfplan
terraform -chdir=infra apply services.tfplan
terraform -chdir=infra output -raw cloudfront_url
terraform -chdir=infra output -raw marimohub_url
terraform -chdir=infra output -raw cognito_user_pool_id
```

Invite your company email in the new Cognito pool using the AWS console.
Complete password setup and email verification, then test login, notebook
execution (`6 * 7`), save, stop and reopen. No users, passwords or notebook data
are automatically copied from the old account.

## Updates and basic notes

- For updates, push a new release, replace the image digests and run normal
  Terraform plan/apply. No staged manifest or wrapper is involved.
- Use the existing authentication, private networking and IAM behavior unchanged.
- Keep credentials, Terraform state/plans and actual tfvars out of Git. Keep a
  private state backup before deployment changes.
- Resources continue billing until stopped/removed. Saving persists notebook
  source/dependency declarations, not Python memory or arbitrary runtime files.
- For hub recovery, disable its service and portal link while retaining foundation
  resources. `marimohub_enabled=false` removes the hub foundation, not just compute.

The stack is still organized by responsibility in `infra/network.tf`, `edge.tf`,
`identity.tf`, `main.tf` and `marimohub*.tf`. File organization does not change
Terraform resource addresses. No application-code changes should be necessary
for another account using this same setup.

## Optional source transfer and tests

A Git clone of source is sufficient. To transfer a clean bundle instead:

```sh
python3 deployment/export_source.py --output /tmp/company-tools.source.tar.gz
```

This optional utility only packages source. It does not deploy anything or add
checks to the Terraform workflow. The bundle omits local state, credentials,
settings, notebook artifacts and personal rollout history; its README is this guide.

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python -m unittest test_app test_marimohub test_deployment -q
```

Terraform tests use mocked AWS calls. The upstream agent and its Apache-2.0
license remain pinned in `marimohub/kernel/`; do not change them for an account move.
