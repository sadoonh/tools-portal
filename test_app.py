"""Real Authlib code flow/JWT validation using test RSA keys and mocked HTTP only."""
import base64
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from authlib.jose import JsonWebKey, jwt
from requests import Response

# Importing the Gunicorn export must also succeed only through explicit opt-in.
with patch.dict(os.environ, {"APP_MODE": "placeholder"}):
    from app import LoginCache, LoginCapacityError, create_app


CONFIG = {
    "TESTING": True,
    "APP_MODE": "cognito",
    "PUBLIC_BASE_URL": "https://dexample123.cloudfront.net",
    "COGNITO_CLIENT_ID": "client123",
    "COGNITO_ISSUER": "https://cognito-idp.us-east-1.amazonaws.com/us-east-1_Test123",
    "COGNITO_DOMAIN": "https://mvp-test.auth.us-east-1.amazoncognito.com",
}
BASE = CONFIG["PUBLIC_BASE_URL"]


class AppTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.key = JsonWebKey.generate_key("RSA", 2048, is_private=True, options={"kid": "test-key"})
        cls.wrong_key = JsonWebKey.generate_key("RSA", 2048, is_private=True, options={"kid": "test-key"})

    def setUp(self):
        self.app = create_app(CONFIG)
        self.client = self.app.test_client()
        self.oauth = self.app.extensions["cognito_client"]
        self.cache = self.app.extensions["login_cache"]
        self.metadata = {
            "issuer": CONFIG["COGNITO_ISSUER"],
            "authorization_endpoint": CONFIG["COGNITO_DOMAIN"] + "/oauth2/authorize",
            "token_endpoint": CONFIG["COGNITO_DOMAIN"] + "/oauth2/token",
            "jwks_uri": CONFIG["COGNITO_ISSUER"] + "/.well-known/jwks.json",
            "id_token_signing_alg_values_supported": ["RS256"],
        }
        self.token = {}
        self.sent = []
        self.send_patch = patch("requests.sessions.Session.send", side_effect=self.send)
        self.send_patch.start()
        self.addCleanup(self.send_patch.stop)

    def send(self, prepared, **kwargs):
        self.sent.append((prepared, kwargs))
        self.assertTrue(kwargs["verify"])
        self.assertEqual(kwargs["timeout"], (3, 5))
        if prepared.url.endswith("/.well-known/openid-configuration"):
            data = self.metadata
        elif prepared.url == self.metadata["jwks_uri"]:
            data = {"keys": [self.key.as_dict()]}
        elif prepared.url == self.metadata["token_endpoint"]:
            data = self.token
        else:
            self.fail("Unexpected network destination")
        response = Response()
        response.status_code = 200
        response._content = json.dumps(data).encode()
        response.headers["Content-Type"] = "application/json"
        response.request = prepared
        return response

    def get(self, path, client=None, **kwargs):
        return (client or self.client).get(path, base_url=BASE, **kwargs)

    def post(self, path, **kwargs):
        return self.client.post(path, base_url=BASE, **kwargs)

    def begin(self, **kwargs):
        response = self.get("/login", **kwargs)
        self.assertEqual(response.status_code, 302)
        query = parse_qs(urlsplit(response.location).query)
        self.state = query["state"][0]
        self.nonce = query["nonce"][0]
        self.state_data = json.loads(self.cache.get("_state_cognito_" + self.state))["data"]
        return response, query

    def prepare_token(self, changes=None, remove=(), key=None, algorithm="RS256"):
        now = int(time.time())
        claims = {
            "iss": CONFIG["COGNITO_ISSUER"], "aud": CONFIG["COGNITO_CLIENT_ID"],
            "sub": "test-subject", "iat": now - 1, "exp": now + 600,
            "nonce": self.nonce, "token_use": "id", "email": "not-retained@example.invalid",
        }
        claims.update(changes or {})
        for name in remove:
            claims.pop(name, None)
        self.token = {
            "id_token": jwt.encode({"alg": algorithm, "kid": "test-key"}, claims, key or self.key).decode(),
            "access_token": "discarded-access-token", "refresh_token": "discarded-refresh-token",
            "token_type": "Bearer", "expires_in": 600,
        }
        return claims

    def callback(self, client=None, state=None):
        return self.get("/callback?code=one-time-code&state=" + (state or self.state), client=client)

    def signed_in(self, changes=None):
        self.begin()
        claims = self.prepare_token(changes)
        self.assertEqual(self.callback().status_code, 302)
        return claims

    def session_data(self):
        cookie = self.client.get_cookie("__Host-mvp_session", domain=urlsplit(BASE).hostname)
        return self.app.session_interface.get_signing_serializer(self.app).loads(cookie.value)

    def test_fail_closed_configuration(self):
        with patch.dict(os.environ, {}, clear=True), self.assertRaises(ValueError):
            create_app()
        for field in ("PUBLIC_BASE_URL", "COGNITO_CLIENT_ID", "COGNITO_ISSUER", "COGNITO_DOMAIN"):
            with self.subTest(field=field), self.assertRaises(ValueError):
                create_app(dict(CONFIG, **{field: ""}))
        for base in ("http://dexample123.cloudfront.net", BASE + "/", "https://evil.example"):
            with self.assertRaises(ValueError):
                create_app(dict(CONFIG, PUBLIC_BASE_URL=base))
        with self.assertRaises(ValueError):
            create_app(dict(CONFIG, APP_MODE="typo"))
        with self.assertRaises(ValueError):
            create_app({"SESSION_COOKIE_SECURE": False})

    def test_placeholder_is_explicit_and_harmless(self):
        client = create_app({"TESTING": True, "APP_MODE": "placeholder"}).test_client()
        self.assertEqual(client.get("/").data.decode(), "MVP connectivity check; authentication not configured.")
        for path in ("/login", "/callback"):
            self.assertEqual(client.get(path).status_code, 503)

    def test_health_anonymous_and_responses_not_cached(self):
        self.assertEqual(self.get("/health").json, {"status": "ok"})
        for path in ("/", "/health", "/logout", "/not-found", "/callback"):
            response = self.get(path)
            self.assertEqual(response.headers["Cache-Control"], "private, no-store")
        self.assertEqual(self.get("/").location, "/login")
        self.assertNotIn(b"Protected welcome", self.get("/").data)

    def test_pkce_and_cookie_binding(self):
        response, query = self.begin(headers={"X-Forwarded-Host": "evil.example", "X-Forwarded-Proto": "http"})
        self.assertEqual(query["response_type"], ["code"])
        self.assertEqual(query["response_mode"], ["query"])
        self.assertEqual(query["redirect_uri"], [BASE + "/callback"])
        self.assertEqual(query["code_challenge_method"], ["S256"])
        digest = hashlib.sha256(self.state_data["code_verifier"].encode()).digest()
        self.assertEqual(query["code_challenge"], [base64.urlsafe_b64encode(digest).rstrip(b"=").decode()])
        data = self.session_data()
        self.assertEqual(set(data), {"_state_cognito_" + self.state})
        self.assertEqual(set(data["_state_cognito_" + self.state]), {"exp"})
        for flag in ("Secure", "HttpOnly", "SameSite=Lax", "Path=/"):
            self.assertIn(flag, response.headers["Set-Cookie"])
        self.assertNotIn("Domain=", response.headers["Set-Cookie"])

    def test_spoofed_host_cannot_change_callback(self):
        _, query = self.begin(headers={"Host": "evil.example"})
        self.assertEqual(query["redirect_uri"], [BASE + "/callback"])

    def test_real_token_exchange_and_minimal_session(self):
        claims = self.signed_in()
        exchange = next(p for p, _ in self.sent if p.method == "POST")
        body = parse_qs(exchange.body)
        self.assertEqual(body["client_id"], [CONFIG["COGNITO_CLIENT_ID"]])
        self.assertEqual(body["code_verifier"], [self.state_data["code_verifier"]])
        self.assertEqual(body["redirect_uri"], [BASE + "/callback"])
        self.assertEqual(body["grant_type"], ["authorization_code"])
        self.assertNotIn("Authorization", exchange.headers)
        self.assertNotIn("client_secret", body)
        data = self.session_data()
        self.assertEqual(set(data), {"_permanent", "sub", "authenticated_at", "session_expires_at", "logout_csrf"})
        self.assertEqual(data["session_expires_at"], claims["exp"])
        self.assertEqual(self.cache.entries, {})
        self.assertIn(b"Protected welcome", self.get("/").data)

    def test_missing_invalid_and_cross_browser_state(self):
        self.begin()
        self.prepare_token()
        for path in ("/callback", "/callback?code=x", "/callback?code=x&state=wrong", f"/callback?code=x&state={self.state}&state=x"):
            self.assertEqual(self.get(path).status_code, 400)
        other = self.app.test_client()
        self.assertEqual(self.callback(client=other).status_code, 400)
        self.assertFalse(any(p.method == "POST" for p, _ in self.sent))
        self.assertIsNotNone(self.cache.get("_state_cognito_" + self.state))
        self.assertEqual(self.callback().status_code, 302)
        self.assertEqual(self.callback().status_code, 400)

    def test_expired_and_restart_transactions(self):
        for expire in (True, False):
            with self.subTest(expire=expire):
                self.begin()
                self.prepare_token()
                if expire:
                    with self.client.session_transaction(base_url=BASE) as data:
                        data["_state_cognito_" + self.state] = {"exp": time.time() - 1}
                else:
                    self.cache.entries.clear()
                self.assertEqual(self.callback().status_code, 400)
                self.assertEqual(self.get("/").location, "/login")

    def test_oauth_error_and_missing_code_consumed(self):
        for query in ("error=access_denied&error_description=never-log-this", "", "code=x&code=y"):
            with self.subTest(query=query):
                self.begin()
                response = self.get(f"/callback?state={self.state}&{query}")
                self.assertEqual(response.status_code, 400)
                self.assertIsNone(self.cache.get("_state_cognito_" + self.state))
                self.assertNotIn(b"never-log-this", response.data)

    def test_negative_claims_real_signature_validation(self):
        cases = [
            ({"nonce": "wrong"}, (), None),
            ({"nonce": "wrong", "nonce_supported": False}, (), None),
            ({"iss": "https://evil.example"}, (), None),
            ({"aud": "otherclient"}, (), None),
            ({"aud": ["otherclient"], "azp": CONFIG["COGNITO_CLIENT_ID"]}, (), None),
            ({"token_use": "access"}, (), None),
            ({"exp": int(time.time()) - 100}, (), None),
            ({"iat": int(time.time()) + 100}, (), None),
            ({"nbf": int(time.time()) + 100}, (), None),
            ({}, ("nonce",), None), ({}, ("token_use",), None),
            ({}, ("sub",), None), ({}, ("exp",), None),
            ({}, (), self.wrong_key),
        ]
        for changes, remove, key in cases:
            with self.subTest(changes=changes, remove=remove, bad_signature=key is not None):
                self.begin()
                self.prepare_token(changes, remove, key)
                self.assertEqual(self.callback().status_code, 400)
                self.assertEqual(self.get("/").location, "/login")
                self.assertEqual(self.cache.entries, {})

    def test_reject_disallowed_algorithm_and_missing_id_token(self):
        self.begin()
        self.prepare_token(algorithm="RS512")
        self.assertEqual(self.callback().status_code, 400)
        self.begin()
        self.token = {"access_token": "not-an-id-token", "token_type": "Bearer"}
        self.assertEqual(self.callback().status_code, 400)
        self.assertEqual(self.get("/").location, "/login")

    def test_metadata_mismatch_fails_closed(self):
        for field in ("issuer", "jwks_uri", "authorization_endpoint", "token_endpoint"):
            with self.subTest(field=field):
                old = self.metadata[field]
                self.metadata[field] = "https://evil.example/"
                self.oauth.server_metadata.clear()
                self.assertEqual(self.get("/login").status_code, 503)
                self.metadata[field] = old

    def test_logout_csrf_and_get_does_not_mutate(self):
        self.signed_in()
        before = self.session_data()
        self.assertEqual(self.get("/logout").status_code, 200)
        self.assertEqual(self.session_data(), before)
        for value in ("", "wrong", "é"):
            self.assertEqual(self.post("/logout", data={"csrf": value}).status_code, 403)
            self.assertIn(b"Protected welcome", self.get("/").data)
        response = self.post("/logout", data={"csrf": before["logout_csrf"]}, headers={"X-Forwarded-Host": "evil.example", "X-Forwarded-Proto": "http"})
        self.assertTrue(response.location.startswith(CONFIG["COGNITO_DOMAIN"] + "/logout?"))
        self.assertEqual(parse_qs(urlsplit(response.location).query)["logout_uri"], [BASE + "/logout"])
        self.assertEqual(self.get("/").location, "/login")

    def test_absolute_expiry_activity_tampering_and_new_worker(self):
        self.signed_in()
        original = self.session_data()
        for _ in range(3):
            self.assertNotIn("Set-Cookie", self.get("/").headers)
            self.assertEqual(self.session_data(), original)
        cookie = self.client.get_cookie("__Host-mvp_session", domain=urlsplit(BASE).hostname)
        with patch("app.time.time", return_value=original["session_expires_at"] + 1):
            self.assertEqual(self.get("/").location, "/login")
        self.client.set_cookie("__Host-mvp_session", cookie.value[:-1] + "!", domain=urlsplit(BASE).hostname)
        self.assertEqual(self.get("/").location, "/login")
        new_client = create_app(CONFIG).test_client()
        new_client.set_cookie("__Host-mvp_session", cookie.value, domain=urlsplit(BASE).hostname)
        self.assertEqual(self.get("/", client=new_client).location, "/login")

    def test_expiry_capped_at_one_hour(self):
        self.signed_in({"exp": int(time.time()) + 7200})
        data = self.session_data()
        self.assertEqual(data["session_expires_at"], data["authenticated_at"] + 3600)

    def test_new_login_replaces_previous_transaction(self):
        self.begin()
        old = self.state
        self.begin()
        self.assertIsNone(self.cache.get("_state_cognito_" + old))
        self.assertEqual(len(self.cache.entries), 1)

    def test_cache_capacity_safely_rejected(self):
        self.cache.max_entries = 0
        self.assertEqual(self.get("/login").status_code, 503)
        self.assertEqual(self.cache.entries, {})

    def test_sensitive_errors_not_logged(self):
        self.begin()
        with (
            patch.object(self.oauth, "fetch_access_token", side_effect=ValueError("sensitive-code-token")),
            self.assertLogs(self.app.logger, level="WARNING") as logs,
        ):
            self.assertEqual(self.callback().status_code, 400)
        self.assertNotIn("sensitive-code-token", str(logs.output))


class CacheTests(unittest.TestCase):
    def test_ttl_capacity_cleanup_and_consume(self):
        now = [0]
        cache = LoginCache(max_entries=1, ttl=300, clock=lambda: now[0])
        cache.set("a", "transaction", 3600)
        self.assertEqual(cache.get("a"), "transaction")
        with self.assertRaises(LoginCapacityError):
            cache.set("b", "next", 300)
        now[0] = 300
        self.assertIsNone(cache.get("a"))
        cache.set("b", "next", 300)
        cache.delete("b")
        self.assertIsNone(cache.get("b"))


class InfrastructureTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("terraform"), "Install pinned Terraform to test infrastructure")
    def test_offline_mock_plans(self):
        root = Path(__file__).resolve().parent / "infra"
        if not (root / ".terraform").is_dir():
            self.skipTest("Run terraform -chdir=infra init first")
        # All AWS operations are mocked. Fictional identifiers never go to AWS.
        # Test files/state are temporary, not part of the deployable root.
        with tempfile.TemporaryDirectory(prefix="mvp-tf-test-") as temporary:
            directory = Path(temporary)
            for source in [*root.glob("*.tf"), root / ".terraform.lock.hcl"]:
                shutil.copy2(source, directory / source.name)
            (directory / ".terraform").symlink_to(root / ".terraform", target_is_directory=True)
            (directory / "tests").mkdir()
            (directory / "tests" / "mvp.tftest.hcl").write_text('''
mock_provider "aws" {
  override_during = plan
  mock_data "aws_availability_zone" {
    defaults = { region = "us-east-1", zone_type = "availability-zone", zone_id = "use1-az1" }
  }
  mock_resource "aws_ecr_repository" {
    defaults = { repository_url = "123456789012.dkr.ecr.us-east-1.amazonaws.com/sdge-mvp-test" }
  }
  mock_resource "aws_cloudfront_distribution" {
    defaults = { domain_name = "dexample123.cloudfront.net" }
  }
}
variables {
  aws_account_id = "123456789012"
  aws_region = "us-east-1"
  project_name = "sdge-mvp-test"
  owner = "test-only"
  availability_zones = ["us-east-1a", "us-east-1b"]
  vpc_cidr = "10.90.0.0/16"
}
run "disabled_first_apply" {
  command = plan
  assert {
    condition = length(aws_ecs_task_definition.app) == 0 && length(aws_ecs_service.app) == 0 && aws_lb.app.internal
    error_message = "First apply must have private origin and no task/image dependency."
  }
  assert {
    condition = aws_cloudfront_vpc_origin.app.vpc_origin_endpoint_config[0].origin_protocol_policy == "http-only" && aws_cloudfront_distribution.app.default_cache_behavior[0].viewer_protocol_policy == "redirect-to-https"
    error_message = "Transport must match approved design."
  }
}
run "placeholder" {
  command = plan
  variables {
    enable_service = true
    image_uri = "123456789012.dkr.ecr.us-east-1.amazonaws.com/sdge-mvp-test@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
  }
  assert {
    condition = aws_ecs_service.app[0].desired_count == 1 && !aws_ecs_service.app[0].network_configuration[0].assign_public_ip && aws_ecs_service.app[0].deployment_minimum_healthy_percent == 0 && aws_ecs_service.app[0].deployment_maximum_percent == 100
    error_message = "Require private single task and stop/start."
  }
  assert {
    condition = [for v in jsondecode(aws_ecs_task_definition.app[0].container_definitions)[0].environment : v.value if v.name == "APP_MODE"] == ["placeholder"]
    error_message = "Connectivity mode must be explicit."
  }
}
run "authenticated" {
  command = plan
  variables {
    enable_service = true
    auth_enabled = true
    image_uri = "123456789012.dkr.ecr.us-east-1.amazonaws.com/sdge-mvp-test@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
  }
  assert {
    condition = aws_cognito_user_pool.app[0].admin_create_user_config[0].allow_admin_create_user_only && !aws_cognito_user_pool_client.app[0].generate_secret && aws_cognito_managed_login_branding.app[0].use_cognito_provided_values && aws_cognito_user_pool_domain.app[0].managed_login_version == 2
    error_message = "Require invite-only public client and v2 branding."
  }
  assert {
    condition = aws_cognito_user_pool_client.app[0].callback_urls == toset(["https://dexample123.cloudfront.net/callback"])
    error_message = "Callback must use canonical HTTPS origin."
  }
}
run "mutable_image_rejected" {
  command = plan
  variables {
    enable_service = true
    image_uri = "123456789012.dkr.ecr.us-east-1.amazonaws.com/sdge-mvp-test:latest"
  }
  expect_failures = [var.image_uri]
}
run "empty_image_rejected" {
  command = plan
  variables { enable_service = true }
  expect_failures = [var.image_uri]
}
run "wrong_repository_rejected" {
  command = plan
  variables {
    enable_service = true
    image_uri = "123456789012.dkr.ecr.us-east-1.amazonaws.com/unrelated@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
  }
  expect_failures = [var.image_uri]
}
run "invalid_task_size_rejected" {
  command = plan
  variables { task_memory = 8192 }
  expect_failures = [var.task_memory]
}
run "public_cidr_rejected" {
  command = plan
  variables { vpc_cidr = "8.8.0.0/16" }
  expect_failures = [var.vpc_cidr]
}
run "excluded_az_rejected" {
  command = plan
  override_data {
    target = data.aws_availability_zone.selected["us-east-1a"]
    values = { region = "us-east-1", zone_type = "availability-zone", zone_id = "use1-az3" }
  }
  expect_failures = [data.aws_availability_zone.selected["us-east-1a"]]
}
''')
            result = subprocess.run(
                ["terraform", f"-chdir={directory}", "test", "-no-color"],
                check=False, capture_output=True, text=True, timeout=120,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("9 passed, 0 failed", result.stdout)


if __name__ == "__main__":
    unittest.main()
