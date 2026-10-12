"""Portal navigation and offline Terraform contracts; never creates AWS resources."""
import os
import shutil
import subprocess
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import test_app as portal_tests
from app import create_app
from marimohub.build_images import export_rootfs, kernel_layer_metadata


class ToolRegistryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        portal_tests.AppTests.setUpClass()

    def client(self, url=""):
        # Reuse the real Authlib/RSA mocked-HTTP login fixture, not forged cookies.
        case = portal_tests.AppTests("runTest")
        with patch.dict(os.environ, {"MARIMOHUB_URL": url}):
            case.setUp()
        self.addCleanup(case.doCleanups)
        return case

    def test_blank_registry_is_authenticated_and_disabled(self):
        case = self.client()
        self.assertEqual(case.get("/").location, "/login")
        case.signed_in()
        response = case.get("/")
        self.assertIn(b"Tools Portal", response.data)
        self.assertIn(b"No tools are enabled yet", response.data)
        self.assertNotIn(b"Marimohub", response.data)
        self.assertIn(b'action="/logout"', response.data)
        self.assertEqual(response.headers["Cache-Control"], "private, no-store")

    def test_link_is_fixed_navigation_after_real_login(self):
        url = "https://dmarimohub123.cloudfront.net"
        case = self.client(url)
        self.assertNotIn(url.encode(), case.get("/").data)
        case.signed_in()
        response = case.get("/", headers={"X-Forwarded-Host": "evil.example"})
        self.assertIn(f'href="{url}" rel="noreferrer"'.encode(), response.data)
        self.assertNotIn(b"evil.example", response.data)
        self.assertNotIn(b"<iframe", response.data)
        self.assertNotIn(b"access-token", response.data)
        self.assertNotIn(b"code_verifier", response.data)
        self.assertNotIn(b"?token=", response.data)

    def test_company_title_is_plain_text_and_autoescaped(self):
        case = self.client()
        # Recreate before routes capture configuration; use real fixture auth flow.
        case.app = create_app(dict(portal_tests.CONFIG, PORTAL_TITLE="Company <Tools>"))
        case.client = case.app.test_client()
        case.oauth = case.app.extensions["cognito_client"]
        case.cache = case.app.extensions["login_cache"]
        case.signed_in()
        response = case.get("/")
        self.assertIn(b"Company &lt;Tools&gt;", response.data)
        self.assertNotIn(b"Company <Tools>", response.data)
        self.assertEqual(response.headers["Cache-Control"], "private, no-store")

    def test_invalid_company_titles_fail_startup(self):
        for title in ("", " ", "x" * 81, "bad\nheader", "bad\rheader"):
            with self.subTest(title=title), self.assertRaises(ValueError):
                create_app(dict(portal_tests.CONFIG, PORTAL_TITLE=title))

    def test_malformed_tool_urls_fail_startup(self):
        for url in ("http://d123.cloudfront.net", "https://evil.example",
                    "https://d123.cloudfront.net/", "https://d123.cloudfront.net/path",
                    "https://user@d123.cloudfront.net", "https://d123.cloudfront.net:443",
                    "https://d123.cloudfront.net?token=secret", "https://d123.cloudfront.net#x",
                    "javascript:alert(1)", 'https://d123.cloudfront.net\" onclick=\"alert(1)',
                    " https://d123.cloudfront.net", "https://d123.cloudfront.net\n"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                create_app(dict(portal_tests.CONFIG, MARIMOHUB_URL=url))

    def test_configured_tool_does_not_leak_through_placeholder(self):
        client = create_app({"APP_MODE": "placeholder", "TESTING": True,
                             "MARIMOHUB_URL": "https://d123.cloudfront.net"}).test_client()
        self.assertNotIn(b"Marimohub", client.get("/").data)
        self.assertNotIn(b"cloudfront.net", client.get("/").data)


class KernelImageTests(unittest.TestCase):
    def test_tmp_layer_is_root_owned_sticky_and_writable(self):
        member = tarfile.TarInfo("./tmp")
        member.type = tarfile.DIRTYPE
        member.mode = 0o755  # Reproduce the incorrectly exported runtime directory.
        member.uid = member.gid = 999
        result = kernel_layer_metadata(member)
        self.assertEqual((result.uid, result.gid, result.mode), (0, 0, 0o1777))

    def test_tmp_must_not_be_a_symlink(self):
        member = tarfile.TarInfo("./tmp")
        member.type = tarfile.SYMTYPE
        member.linkname = "/root"
        with self.assertRaises(ValueError):
            kernel_layer_metadata(member)

    def test_metadata_preserves_scoped_ownership_and_excludes_runtime_contents(self):
        for name, owner in [("./workspace", 10001), ("./home/notebook", 10001),
                            ("./opt/marimohub/fargate_agent.py", 10001), ("./etc", 0),
                            ("./usr/local/bin/python3", 0)]:
            with self.subTest(name=name):
                member = tarfile.TarInfo(name)
                member.mode = 0o755
                result = kernel_layer_metadata(member)
                self.assertEqual((result.uid, result.gid, result.mode), (owner, owner, 0o755))
        for name in ("./tmp/.marimohub-kernel-token", "./proc/self/environ", "./dev/null", "./run/probe"):
            self.assertIsNone(kernel_layer_metadata(tarfile.TarInfo(name)))

    def test_export_repairs_missing_or_nonwritable_tmp(self):
        for existing in (False, True):
            with self.subTest(existing=existing), tempfile.TemporaryDirectory() as directory:
                def fake_run(*args, existing=existing):
                    if args[0] == "tar" and existing:
                        path = Path(directory) / "rootfs/tmp"
                        path.mkdir()
                        path.chmod(0o755)
                with patch("marimohub.build_images.run", side_effect=fake_run):
                    rootfs = export_rootfs("test-image-not-fetched", Path(directory))
                self.assertEqual((rootfs / "tmp").stat().st_mode & 0o7777, 0o1777)


class HubInfrastructureTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("terraform"), "Install pinned Terraform")
    def test_offline_hub_plans(self):
        root = Path(__file__).resolve().parent / "infra"
        if not (root / ".terraform").is_dir():
            self.skipTest("Run terraform -chdir=infra init first")
        with tempfile.TemporaryDirectory(prefix="hub-tf-test-") as temporary:
            directory = Path(temporary)
            for source in [*root.glob("*.tf"), root / ".terraform.lock.hcl"]:
                shutil.copy2(source, directory / source.name)
            (directory / ".terraform").symlink_to(root / ".terraform", target_is_directory=True)
            (directory / "tests").mkdir()
            (directory / "tests/hub.tftest.hcl").write_text(HUB_TERRAFORM_TESTS)
            result = subprocess.run(
                ["terraform", f"-chdir={directory}", "test", "-no-color"],
                capture_output=True, text=True, timeout=180, check=False,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("10 passed, 0 failed", result.stdout)


HUB_TERRAFORM_TESTS = '''
mock_provider "aws" {
  override_during = plan
  mock_data "aws_availability_zone" {
    defaults = { region = "us-east-1", zone_type = "availability-zone", zone_id = "use1-az1" }
  }
  mock_resource "aws_ecr_repository" {
    defaults = { repository_url = "123456789012.dkr.ecr.us-east-1.amazonaws.com/sdge-mvp-test" }
  }
  mock_resource "aws_cloudfront_distribution" {
    defaults = { domain_name = "dportal123.cloudfront.net" }
  }
  mock_resource "aws_cloudfront_vpc_origin" {
    defaults = { id = "vo-test" }
  }
  mock_resource "aws_cloudfront_response_headers_policy" {
    defaults = { id = "hub-headers-policy" }
  }
  mock_resource "aws_cognito_user_pool" {
    defaults = { id = "us-east-1_Test123" }
  }
  mock_resource "aws_cognito_user_pool_client" {
    defaults = { id = "test-client" }
  }
  mock_resource "aws_ecs_cluster" {
    defaults = { arn = "arn:aws:ecs:us-east-1:123456789012:cluster/sdge-mvp-test" }
  }
  mock_resource "aws_ecs_task_definition" {
    defaults = { arn = "arn:aws:ecs:us-east-1:123456789012:task-definition/sdge-mvp-test-kernel:1" }
  }
  mock_resource "aws_security_group" {
    defaults = { id = "sg-1234567890abcdef0" }
  }
  mock_resource "aws_subnet" {
    defaults = { id = "subnet-1234567890abcdef0" }
  }
  mock_resource "aws_s3_bucket" {
    defaults = { id = "sdge-mvp-test-notebooks", arn = "arn:aws:s3:::sdge-mvp-test-notebooks" }
  }
  mock_resource "aws_cloudwatch_log_group" {
    defaults = { arn = "arn:aws:logs:us-east-1:123456789012:log-group:test", name = "/ecs/test" }
  }
  mock_resource "aws_secretsmanager_secret" {
    defaults = { arn = "arn:aws:secretsmanager:us-east-1:123456789012:secret:test-123456" }
  }
  mock_resource "aws_iam_role" {
    defaults = { arn = "arn:aws:iam::123456789012:role/test-role" }
  }
}
# Random ephemeral values run locally; Terraform cannot mock ephemeral types.
# Only AWS is mocked, and there are no real AWS operations.
variables {
  aws_account_id = "123456789012"
  aws_region = "us-east-1"
  project_name = "sdge-mvp-test"
  owner = "test-only"
  availability_zones = ["us-east-1a", "us-east-1b"]
  vpc_cidr = "10.90.0.0/16"
  auth_enabled = true
  marimohub_allowed_email_domains = ["example.com"]
  enable_service = true
  image_uri = "123456789012.dkr.ecr.us-east-1.amazonaws.com/sdge-mvp-test@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
  marimohub_server_image_uri = "123456789012.dkr.ecr.us-east-1.amazonaws.com/sdge-mvp-test-hub@sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
  marimohub_kernel_image_uri = "123456789012.dkr.ecr.us-east-1.amazonaws.com/sdge-mvp-test-kernel@sha256:cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc"
}
run "disabled_preserves_portal" {
  command = plan
  assert {
    condition = length(aws_ecs_service.hub) == 0 && length(aws_s3_bucket.hub) == 0 && length(aws_ecr_repository.hub_images) == 0 && length(aws_cognito_user_pool_client.hub) == 0 && length(aws_cloudfront_distribution.hub) == 0
    error_message = "Disabled extension must create no hub infrastructure."
  }
  assert {
    condition = [for v in jsondecode(aws_ecs_task_definition.app[0].container_definitions)[0].environment : v.value if v.name == "MARIMOHUB_URL"] == [] && !aws_cognito_user_pool_client.app[0].generate_secret
    error_message = "Portal auth/client/environment must remain unchanged until launcher rollout."
  }
}
run "foundation_without_images_or_tasks" {
  command = plan
  variables {
    marimohub_enabled = true
    marimohub_server_image_uri = ""
    marimohub_kernel_image_uri = ""
  }
  assert {
    condition = length(aws_ecs_service.hub) == 0 && length(aws_ecs_task_definition.kernel) == 0 && length(aws_ecr_repository.hub_images) == 2 && !aws_s3_bucket.hub[0].force_destroy && aws_s3_bucket_versioning.hub[0].versioning_configuration[0].status == "Enabled"
    error_message = "Foundation must work without images and retain/version private notebook storage."
  }
  assert {
    condition = aws_s3_bucket_public_access_block.hub[0].block_public_acls && aws_s3_bucket_public_access_block.hub[0].block_public_policy && aws_s3_bucket_public_access_block.hub[0].ignore_public_acls && aws_s3_bucket_public_access_block.hub[0].restrict_public_buckets && aws_cognito_user_pool_client.hub[0].generate_secret
    error_message = "Storage must remain private and hub must have its own confidential client."
  }
  assert {
    condition = one(aws_cloudfront_distribution.hub[0].origin).vpc_origin_config[0].vpc_origin_id == aws_cloudfront_vpc_origin.app.id && aws_lb_target_group.hub[0].port == 3000 && aws_lb_listener_rule.hub[0].priority == 100
    error_message = "Hub must reuse private VPC origin/ALB while routing to a distinct target."
  }
  assert {
    condition = aws_cloudfront_distribution.hub[0].default_cache_behavior[0].response_headers_policy_id == aws_cloudfront_response_headers_policy.hub[0].id && one(aws_cloudfront_response_headers_policy.hub[0].custom_headers_config[0].items).value == "private, no-store" && one(aws_cloudfront_response_headers_policy.hub[0].custom_headers_config[0].items).override
    error_message = "Both edge and browser caches must avoid retaining hub private responses."
  }
}
run "private_native_runtime" {
  command = plan
  variables {
    marimohub_enabled = true
    marimohub_service_enabled = true
    marimohub_proxy_ack = true
  }
  assert {
    condition = aws_ecs_service.hub[0].desired_count == 1 && !aws_ecs_service.hub[0].network_configuration[0].assign_public_ip && aws_ecs_service.hub[0].deployment_minimum_healthy_percent == 0 && aws_ecs_service.hub[0].deployment_maximum_percent == 100 && aws_ecs_task_definition.kernel[0].cpu == "1024" && aws_ecs_task_definition.kernel[0].memory == "2048"
    error_message = "Single nonoverlapping hub and fixed private kernels are required."
  }
  assert {
    condition = alltrue([for container in [jsondecode(aws_ecs_task_definition.hub[0].container_definitions)[0], jsondecode(aws_ecs_task_definition.kernel[0].container_definitions)[0]] : container.user == "10001:10001"])
    error_message = "Hub and kernel must not run as root."
  }
  assert {
    condition = [for v in jsondecode(aws_ecs_task_definition.hub[0].container_definitions)[0].environment : v.value if v.name == "MARIMOHUB_DEFAULT_ROLE"] == ["none"] && [for v in jsondecode(aws_ecs_task_definition.hub[0].container_definitions)[0].environment : v.value if v.name == "MARIMOHUB_AUTH_OIDC_EMAIL_VERIFICATION"] == ["required"] && [for v in jsondecode(aws_ecs_task_definition.hub[0].container_definitions)[0].environment : v.value if v.name == "MARIMOHUB_SANDBOX_AUTH"] == ["on"]
    error_message = "Ownership, verified identity and native kernel auth must fail closed."
  }
  assert {
    condition = [for v in jsondecode(aws_ecs_task_definition.hub[0].container_definitions)[0].environment : v.value if contains(["MARIMOHUB_AUTH_SESSION_SECRET", "MARIMOHUB_AUTH_OIDC_CLIENT_SECRET", "MARIMOHUB_COMPUTE_FARGATE_AGENT_SECRET", "MARIMOHUB_COMPUTE_IMAGE"], v.name)] == [] && length(jsondecode(aws_ecs_task_definition.hub[0].container_definitions)[0].secrets) == 3
    error_message = "No literal hub secrets or per-notebook image overrides in task environment."
  }
  assert {
    condition = jsondecode(aws_iam_role_policy.hub_runtime[0].policy).Statement[4].Condition.StringEquals["aws:ResourceTag/marimohub:owner"] == "sdge-mvp-test-marimohub" && jsondecode(aws_iam_role_policy.hub_runtime[0].policy).Statement[8].Condition.StringEquals["iam:PassedToService"] == "ecs-tasks.amazonaws.com" && length(jsondecode(aws_iam_role_policy.hub_runtime[0].policy).Statement[8].Resource) == 2
    error_message = "Only owned kernels and exact kernel roles may be controlled."
  }
  assert {
    condition = toset([for rule in aws_vpc_security_group_ingress_rule.kernel : rule.from_port]) == toset([2717, 2718]) && [for port in jsondecode(aws_ecs_task_definition.kernel[0].container_definitions)[0].portMappings : port.containerPort] == [2717, 2718]
    error_message = "Only private agent/marimo ports are allowed."
  }
}
run "portal_link_is_opt_in" {
  command = plan
  variables {
    marimohub_enabled = true
    marimohub_service_enabled = true
    marimohub_proxy_ack = true
    marimohub_portal_link_enabled = true
  }
  assert {
    condition = [for v in jsondecode(aws_ecs_task_definition.app[0].container_definitions)[0].environment : v.value if v.name == "MARIMOHUB_URL"] == ["https://dportal123.cloudfront.net"] && [for v in jsondecode(aws_ecs_task_definition.app[0].container_definitions)[0].environment : v.value if v.name == "APP_MODE"] == ["cognito"]
    error_message = "Link is fixed and portal auth must remain enabled."
  }
}
run "missing_ack_rejected" {
  command = plan
  variables {
    marimohub_enabled = true
    marimohub_service_enabled = true
  }
  expect_failures = [var.marimohub_service_enabled]
}
run "service_without_foundation_rejected" {
  command = plan
  variables {
    marimohub_service_enabled = true
    marimohub_proxy_ack = true
  }
  expect_failures = [var.marimohub_service_enabled]
}
run "link_without_hub_rejected" {
  command = plan
  variables { marimohub_portal_link_enabled = true }
  expect_failures = [var.marimohub_portal_link_enabled]
}
run "mutable_hub_image_rejected" {
  command = plan
  variables {
    marimohub_enabled = true
    marimohub_service_enabled = true
    marimohub_proxy_ack = true
    marimohub_server_image_uri = "123456789012.dkr.ecr.us-east-1.amazonaws.com/sdge-mvp-test-hub:latest"
  }
  expect_failures = [var.marimohub_server_image_uri]
}
run "foreign_kernel_image_rejected" {
  command = plan
  variables {
    marimohub_enabled = true
    marimohub_service_enabled = true
    marimohub_proxy_ack = true
    marimohub_kernel_image_uri = "123456789012.dkr.ecr.us-east-1.amazonaws.com/unrelated@sha256:cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc"
  }
  expect_failures = [var.marimohub_kernel_image_uri]
}
run "wildcard_admission_rejected" {
  command = plan
  variables { marimohub_allowed_email_domains = ["*"] }
  expect_failures = [var.marimohub_allowed_email_domains]
}
'''


if __name__ == "__main__":
    unittest.main()
