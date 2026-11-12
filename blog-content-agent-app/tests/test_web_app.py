import json
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from unittest.mock import patch

import web_app


class WebAppTests(unittest.TestCase):
    def test_provider_adapters_use_request_key_and_schema(self):
        payload = {"mode": "plan", "tone": "A", "target": "과정", "purpose": "소개",
                   "audience": "준비생", "course_url": "", "topic_count": 1,
                   "evidence": "", "notes": ""}
        sample = {"topics": [], "common_sources": [], "method_note": "test"}
        with patch.object(web_app.agent, "build_prompt", return_value="prompt"), \
             patch.object(web_app, "post_json", return_value={"output": [{"type": "message", "content": [{"type": "output_text", "text": json.dumps(sample)}]}]}) as request:
            marker = web_app.CURRENT_CREDENTIAL.set(("openai", "secret-1", "gpt-5.4"))
            try:
                self.assertEqual(web_app.call_provider(payload), sample)
            finally:
                web_app.CURRENT_CREDENTIAL.reset(marker)
            self.assertEqual(request.call_args.args[2]["Authorization"], "Bearer secret-1")
        with patch.object(web_app.agent, "build_prompt", return_value="prompt"), \
             patch.object(web_app, "post_json", return_value={"steps": [{"type": "model_output", "content": [{"type": "text", "text": json.dumps(sample)}]}]}) as request:
            marker = web_app.CURRENT_CREDENTIAL.set(("gemini", "secret-2", "gemini-3.8-flash"))
            try:
                self.assertEqual(web_app.call_provider(payload), sample)
            finally:
                web_app.CURRENT_CREDENTIAL.reset(marker)
            self.assertEqual(request.call_args.args[2]["x-goog-api-key"], "secret-2")
        with patch.object(web_app.agent, "build_prompt", return_value="prompt"), \
             patch.object(web_app, "post_json", side_effect=[
                 {"content": [{"type": "text", "text": "https://example.org"}]},
                 {"content": [{"type": "text", "text": json.dumps(sample)}]},
             ]) as request:
            marker = web_app.CURRENT_CREDENTIAL.set(("claude", "secret-3", "claude-sonnet-5"))
            try:
                self.assertEqual(web_app.call_provider(payload), sample)
            finally:
                web_app.CURRENT_CREDENTIAL.reset(marker)
            self.assertEqual(request.call_count, 2)
            self.assertEqual(request.call_args_list[0].args[2]["x-api-key"], "secret-3")

    def test_demo_and_key_rejection(self):
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), web_app.HostedHandler)
        worker = threading.Thread(target=httpd.serve_forever, daemon=True)
        worker.start()
        base = f"http://127.0.0.1:{httpd.server_port}"
        form = {"mode": "full", "tone": "A", "target": "과정", "purpose": "소개",
                "audience": "준비생", "course_url": "", "topic_count": 1,
                "evidence": "", "notes": ""}
        try:
            with patch.dict("os.environ", {"PUBLIC_ORIGIN": "https://example.org"}):
                status = json.load(urllib.request.urlopen(base + "/api/status"))
                self.assertIn("gemini", status["providers"])
                request = urllib.request.Request(base + "/api/demo", data=json.dumps(form).encode(),
                                                 headers={"Origin": "https://example.org", "Content-Type": "application/json"})
                result = json.load(urllib.request.urlopen(request))
                self.assertTrue(result["demo"])
                bad = urllib.request.Request(base + "/api/generate", data=json.dumps({**form, "provider": "openai", "model": "gpt-5.4", "api_key": "key_not_secret"}).encode(),
                                             headers={"Origin": "https://example.org", "Content-Type": "application/json"})
                with self.assertRaises(urllib.error.HTTPError) as error:
                    urllib.request.urlopen(bad)
                self.assertEqual(error.exception.code, 400)
                self.assertNotIn("key_not_secret", error.exception.read().decode())
        finally:
            httpd.shutdown()
            httpd.server_close()


if __name__ == "__main__":
    unittest.main()

