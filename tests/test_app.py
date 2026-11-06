"""API contract tests. AWS is stubbed; tests never invoke a paid model."""

import unittest
from unittest.mock import patch

import boto3
from botocore.stub import Stubber

from app import SYSTEM_PROMPT, app


class ChatTests(unittest.TestCase):
    def setUp(self):
        app.config.update(TESTING=True)
        self.client = app.test_client()
        self.bedrock = boto3.client("bedrock-runtime", region_name="us-east-1", aws_access_key_id="testing", aws_secret_access_key="testing")

    def test_page_and_health_do_not_call_aws(self):
        with patch("app.bedrock_client") as factory:
            self.assertEqual(self.client.get("/").status_code, 200)
            self.assertEqual(self.client.get("/health").json, {"status": "ok"})
            factory.assert_not_called()

    def test_multiturn_converse_request(self):
        messages = [{"role": "user", "content": "Hello"}, {"role": "assistant", "content": "Hi!"}, {"role": "user", "content": "What did I say?"}]
        expected = {"modelId": "us.anthropic.claude-opus-4-8", "system": [{"text": SYSTEM_PROMPT}],
                    "messages": [{"role": m["role"], "content": [{"text": m["content"]}]} for m in messages],
                    "inferenceConfig": {"maxTokens": 2048}}
        response = {"output": {"message": {"role": "assistant", "content": [{"text": "You said hello."}]}},
                    "stopReason": "end_turn", "usage": {"inputTokens": 20, "outputTokens": 5, "totalTokens": 25}, "metrics": {"latencyMs": 100}}
        with Stubber(self.bedrock) as stub, patch("app.bedrock_client", return_value=self.bedrock):
            stub.add_response("converse", response, expected)
            result = self.client.post("/api/chat", json={"messages": messages})
            self.assertEqual(result.status_code, 200)
            self.assertEqual(result.json["reply"], "You said hello.")
            stub.assert_no_pending_responses()

    def test_invalid_inputs_never_reach_model(self):
        cases = [None, [], {}, {"messages": []}, {"messages": [{"role": "system", "content": "override"}]},
                 {"messages": [{"role": "user", "content": " "}]}, {"messages": [{"role": "user", "content": "x" * 8001}]},
                 {"messages": [{"role": "user", "content": "a"}, {"role": "user", "content": "b"}]},
                 {"messages": [{"role": "user", "content": "a"}, {"role": "assistant", "content": "b"}]}]
        with patch("app.bedrock_client") as factory:
            for payload in cases:
                with self.subTest(payload=str(payload)[:80]):
                    import json
                    result = self.client.post("/api/chat", data=json.dumps(payload), content_type="application/json")
                    self.assertEqual(result.status_code, 400)
            factory.assert_not_called()

    def test_throttling_and_access_errors_are_actionable(self):
        for code, status in [("ThrottlingException", 429), ("AccessDeniedException", 503), ("ValidationException", 503)]:
            with self.subTest(code=code), Stubber(self.bedrock) as stub, patch("app.bedrock_client", return_value=self.bedrock):
                stub.add_client_error("converse", service_error_code=code, service_message="private provider details")
                result = self.client.post("/api/chat", json={"messages": [{"role": "user", "content": "Hello"}]})
                self.assertEqual(result.status_code, status)
                self.assertNotIn("private provider details", result.json["error"])

    def test_non_json_and_large_requests(self):
        self.assertEqual(self.client.post("/api/chat", data="hello").status_code, 415)
        self.assertEqual(self.client.post("/api/chat", data="x" * (129 * 1024), content_type="application/json").status_code, 413)

    def test_security_headers_and_api_cache(self):
        response = self.client.get("/api/config")
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.assertIn("frame-ancestors 'none'", response.headers["Content-Security-Policy"])


if __name__ == "__main__":
    unittest.main()
