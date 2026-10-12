"""Direct Terraform company template and optional source transfer; no real AWS writes."""
import hashlib
import json
import re
import shutil
import subprocess
import tarfile
import tempfile
import unittest
from pathlib import Path

from deployment import export_source as transfer


class DeploymentTests(unittest.TestCase):
    def test_company_template_is_native_terraform_with_auth_and_services_off(self):
        template = (transfer.ROOT / "infra/company.tfvars.example").read_text()
        for name, value in (("auth_enabled", "true"), ("marimohub_enabled", "true"),
                            ("enable_service", "false"), ("marimohub_service_enabled", "false"),
                            ("marimohub_portal_link_enabled", "false"), ("marimohub_proxy_ack", "false")):
            self.assertRegex(template, rf"(?m)^\s*{name}\s*=\s*{value}\s*$")
        self.assertIn("REPLACE_WITH_12_DIGIT_ACCOUNT_ID", template)
        self.assertRegex(template, r'marimohub_allowed_email_domains\s*=\s*\["example.com"\]')
        self.assertFalse((transfer.ROOT / "deployment/deploy.py").exists())
        self.assertFalse((transfer.ROOT / "deployment/company.example.json").exists())

    @unittest.skipUnless(shutil.which("git"), "Git is needed to test its ignore behavior")
    def test_git_ignores_real_configuration_but_keeps_examples_and_source(self):
        ignored = [
            "infra/terraform.tfvars", "infra/terraform.tfvars.json", "infra/images.auto.tfvars.json",
            "infra/terraform.tfstate", "infra/terraform.tfstate.backup", "infra/services.tfplan",
            "infra/services.tfplan.json", ".env", ".env.local", ".aws/config", ".aws/credentials",
            ".aws/sso/cache/session.json", ".docker/config.json", ".terraform/providers/cache",
        ]
        tracked = ["infra/company.tfvars.example", "infra/main.tf", "infra/.terraform.lock.hcl",
                   "app.py", "portal/templates/dashboard.html"]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            subprocess.run(["git", "init", "-q", str(path)], check=True, capture_output=True)
            shutil.copyfile(transfer.ROOT / ".gitignore", path / ".gitignore")
            result = subprocess.run(["git", "-C", str(path), "check-ignore", "--no-index", "--stdin"],
                                    input="\n".join(ignored + tracked) + "\n", capture_output=True,
                                    text=True, check=False)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(set(result.stdout.splitlines()), set(ignored))

    def test_source_export_contains_direct_deployment_without_pilot_data(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "company.source.tar.gz"
            checksum = transfer.export_source(output)
            self.assertEqual(hashlib.sha256(output.read_bytes()).hexdigest(), checksum)
            self.assertEqual(output.stat().st_mode & 0o777, 0o600)
            with tarfile.open(output) as source:
                names = source.getnames()
                self.assertIn("infra/company.tfvars.example", names)
                self.assertIn("infra/network.tf", names)
                self.assertIn("portal/templates/dashboard.html", names)
                self.assertIn("marimohub/kernel/LICENSE", names)
                self.assertNotIn("deployment/deploy.py", names)
                self.assertNotIn("marimohub/kernel/README.md", names)
                self.assertNotIn("marimohub_mvp_plan.md", names)
                self.assertFalse(any("tfstate" in n or ".venv" in n or "auto.tfvars" in n for n in names))
                self.assertEqual(source.extractfile("README.md").read(), (transfer.ROOT / "docs/company-deployment.md").read_bytes())
                hashes = json.load(source.extractfile("SOURCE_SHA256.json"))
                for name, digest in hashes.items():
                    self.assertEqual(hashlib.sha256(source.extractfile(name).read()).hexdigest(), digest)
                for member in source.getmembers():
                    self.assertEqual((member.uid, member.gid, member.uname, member.gname), (0, 0, "", ""))
                    text = source.extractfile(member).read()
                    self.assertNotIn(b"706059" + b"253616", text, member.name)
                    self.assertNotIn(b"sadoonbusiness" + b"@gmail.com", text, member.name)
            with self.assertRaises(ValueError):
                transfer.export_source(output)

    def test_source_allowlist_rejects_symlinked_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in transfer.source_files():
                target = root / name
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes((transfer.ROOT / name).read_bytes())
            target = root / "app.py"
            target.unlink()
            target.symlink_to(transfer.ROOT / "app.py")
            with self.assertRaises(ValueError):
                transfer.source_files(root)

    def test_bundle_cannot_overwrite_source_checkout(self):
        with self.assertRaises(ValueError):
            transfer.export_source(transfer.ROOT / "company.source.tar.gz")

    def test_company_template_offline_foundation_and_runtime_plans(self):
        if not shutil.which("terraform") or not (transfer.ROOT / "infra/.terraform").is_dir():
            self.skipTest("Initialize source Terraform providers to run mocked plans")
        import test_marimohub
        script = test_marimohub.HUB_TERRAFORM_TESTS.replace("123456789012", "210987654321").replace(
            "us-east-1", "eu-west-1").replace("use1-az1", "euw1-az1").replace("sdge-mvp-test", "company-tools")
        # Existing fixture tests cover disabled defaults; the example enables
        # foundation by default. Override only that default in the old fixture.
        script = script.replace("  auth_enabled = true", "  auth_enabled = true\n  marimohub_enabled = false")
        script = script.replace('run "disabled_preserves_portal" {', '''run "company_template_foundation" {
  command = plan
  variables {
    enable_service = false
    marimohub_enabled = true
    image_uri = ""
    marimohub_server_image_uri = ""
    marimohub_kernel_image_uri = ""
  }
  assert {
    condition = length(aws_ecs_service.app) == 0 && length(aws_ecs_service.hub) == 0 && length(aws_ecs_task_definition.kernel) == 0 && length(aws_ecr_repository.hub_images) == 2 && length(aws_cognito_user_pool.app) == 1
    error_message = "Example must support infrastructure before image publishing."
  }
}
run "company_template_runtime" {
  command = plan
  variables {
    marimohub_enabled = true
    marimohub_service_enabled = true
    marimohub_proxy_ack = true
    marimohub_portal_link_enabled = true
  }
  assert {
    condition = [for v in jsondecode(aws_ecs_task_definition.app[0].container_definitions)[0].environment : v.value if v.name == "PORTAL_TITLE"] == ["Company Tools"] && [for v in jsondecode(aws_ecs_task_definition.app[0].container_definitions)[0].environment : v.value if v.name == "APP_MODE"] == ["cognito"]
    error_message = "Account-specific settings must not require app code changes."
  }
  assert {
    condition = !aws_lb.app.enable_deletion_protection && aws_cognito_user_pool.app[0].deletion_protection == "INACTIVE" && aws_ecr_repository.app.force_delete
    error_message = "Company example should preserve original deployment behavior."
  }
}
run "disabled_preserves_portal" {''')
        template = (transfer.ROOT / "infra/company.tfvars.example").read_text().replace(
            "REPLACE_WITH_12_DIGIT_ACCOUNT_ID", "210987654321").replace("us-east-1", "eu-west-1")
        # No configuration generator or deployment framework: native .tfvars.
        self.assertTrue(re.search(r'aws_region\s*=\s*"eu-west-1"', template))
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            for source in [*(transfer.ROOT / "infra").glob("*.tf"), transfer.ROOT / "infra/.terraform.lock.hcl"]:
                shutil.copy2(source, path / source.name)
            (path / "terraform.tfvars").write_text(template)
            (path / ".terraform").symlink_to(transfer.ROOT / "infra/.terraform", target_is_directory=True)
            (path / "tests").mkdir()
            (path / "tests/company.tftest.hcl").write_text(script)
            result = subprocess.run(["terraform", f"-chdir={path}", "test", "-no-color"],
                                    capture_output=True, text=True, timeout=180, check=False)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("12 passed, 0 failed", result.stdout)


if __name__ == "__main__":
    unittest.main()
