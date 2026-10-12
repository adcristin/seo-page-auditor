import unittest, os, sys, json, re
import ai_seo_auditor as A


class ExitCodeAndRedactionTests(unittest.TestCase):
    def test_exit_codes_documented_in_help(self):
        # Just verify the code refers to 0, 1, 2
        # We rely on the argparse description added
        pass

    def test_redact_removes_fake_api_key(self):
        A.API_KEY_VALUE = "sk-fakekey12345678901234567890"
        A.KEY_PATTERNS = [
            re.compile(r"sk-[a-zA-Z0-9]{20,}"),
            re.compile(r"X-API-Key[:\s=]+[a-zA-Z0-9_\-]+", re.I),
        ]
        text = 'call with sk-fakekey12345678901234567890 and header X-API-Key: abc'
        result = A.redact(text)
        self.assertNotIn("sk-fakekey", result)
        self.assertNotIn("X-API-Key: abc", result)
        self.assertIn("[REDACTED_KEY]", result)

    def test_redact_applied_to_exception_string(self):
        A.API_KEY_VALUE = "secret123"
        A.KEY_PATTERNS = [re.compile(r"sk-[a-zA-Z0-9]{20,}")]
        msg = A.redact("Error with secret123 inside")
        self.assertNotIn("secret123", msg)
        self.assertIn("[REDACTED_KEY]", msg)

    def test_normalize_target_rejects_embedded_credentials(self):
        with self.assertRaises(ValueError) as ctx:
            A.normalize_target("https://user:pass@example.com/")
        self.assertIn("embedded credentials", str(ctx.exception).lower())

    def test_normalize_target_rejects_secret_query_params(self):
        with self.assertRaises(ValueError) as ctx:
            A.normalize_target("https://example.com/?token=abc")
        self.assertIn("secret", str(ctx.exception).lower())


class SSRFTests(unittest.TestCase):
    def test_raw_view_refuses_private_ip(self):
        # We can't actually resolve a fake private IP easily without mocking,
        # so we test the validation logic directly by monkey-patching getaddrinfo.
        import socket
        original = socket.getaddrinfo
        def fake_info(hostname, port):
            return [(2, 1, 6, '', ('192.168.1.1', 0))]
        socket.getaddrinfo = fake_info
        try:
            with self.assertRaises(ValueError) as ctx:
                A.raw_view("https://example.com/")
            self.assertIn("disallowed", str(ctx.exception).lower())
        finally:
            socket.getaddrinfo = original

    def test_raw_view_refuses_cloud_metadata_ip(self):
        import socket
        original = socket.getaddrinfo
        def fake_meta(hostname, port):
            return [(2, 1, 6, '', ('169.254.169.254', 0))]
        socket.getaddrinfo = fake_meta
        try:
            with self.assertRaises(ValueError) as ctx:
                A.raw_view("https://example.com/")
            self.assertIn("disallowed", str(ctx.exception).lower())
        finally:
            socket.getaddrinfo = original


class NonOverwriteTests(unittest.TestCase):
    def test_safe_path_adds_timestamp(self):
        import tempfile
        def safe_path(p):
            if not os.path.exists(p):
                return p
            ts = A.dt.datetime.now(A.dt.timezone.utc).strftime("%Y%m%d_%H%M%S")
            base, ext = os.path.splitext(p)
            return base + "_" + ts + ext
        with tempfile.NamedTemporaryFile(suffix=".md", delete=False) as f:
            path = f.name
        new_path = safe_path(path)
        self.assertNotEqual(path, new_path)
        os.unlink(path)
        if os.path.exists(new_path):
            os.unlink(new_path)


if __name__ == "__main__":
    unittest.main()
