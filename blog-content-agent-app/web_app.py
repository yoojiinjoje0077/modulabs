"""HTTPS-hosted, bring-your-own-key edition of the blog agent.

Run behind a TLS-terminating reverse proxy (for example Render). Keys are
accepted for one request only and are never written to disk or log output.
"""

from __future__ import annotations

import contextvars
import json
import os
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import server as agent


ROOT = Path(__file__).resolve().parent
CURRENT_CREDENTIAL = contextvars.ContextVar("credential", default=None)
MODELS = {
    "openai": ["gpt-6-astra", "gpt-5.4"],
    "gemini": ["gemini-3.8-flash", "gemini-3.1-pro-preview"],
    "claude": ["claude-sonnet-5", "claude-opus-4-8"],
}


def provider_error(provider: str, exc: urllib.error.HTTPError) -> RuntimeError:
    # Provider messages can echo request fields; never reflect them to users.
    if exc.code in (401, 403):
        return RuntimeError(f"{provider} API 인증에 실패했습니다. 본인의 비밀 API 키와 모델 사용 권한을 확인하세요.")
    if exc.code == 429:
        return RuntimeError(f"{provider} API 사용량 또는 결제 한도에 도달했습니다.")
    return RuntimeError(f"{provider} API 요청이 실패했습니다 ({exc.code}). 모델과 계정 설정을 확인하세요.")


def post_json(provider: str, url: str, headers: dict, body: dict) -> dict:
    request = urllib.request.Request(url, data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
                                     headers={**headers, "Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=240) as response:
            return json.load(response)
    except urllib.error.HTTPError as exc:
        raise provider_error(provider, exc) from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"{provider} API에 연결하지 못했습니다.") from exc


def call_provider(data: dict) -> dict:
    credential = CURRENT_CREDENTIAL.get()
    if not credential:
        raise RuntimeError("생성 요청에 API 키가 없습니다.")
    provider, key, model = credential
    prompt = agent.build_prompt(data)
    schema = agent.PLAN_SCHEMA if data["mode"] == "plan" else agent.ARTICLE_SCHEMA
    instructions = agent.BASE_INSTRUCTIONS + "\n반환 형식은 지정된 JSON 스키마를 따르세요. 검색에서 확인한 출처 URL만 쓰세요."

    if provider == "openai":
        body = {"model": model, "instructions": instructions, "input": prompt,
                "tools": [{"type": "web_search"}],
                "text": {"format": {"type": "json_schema", "name": "blog_content", "strict": True, "schema": schema}},
                "store": False}
        response = post_json("OpenAI", "https://api.openai.com/v1/responses",
                             {"Authorization": f"Bearer {key}"}, body)
        return json.loads(agent.extract_text(response))

    if provider == "gemini":
        body = {"model": model, "input": instructions + "\n\n" + prompt,
                "tools": [{"type": "google_search"}],
                "response_format": {"type": "text", "mime_type": "application/json", "schema": schema}}
        response = post_json("Gemini", "https://generativelanguage.googleapis.com/v1beta/interactions",
                             {"x-goog-api-key": key}, body)
        interaction = response.get("interaction", response)
        texts = []
        for step in interaction.get("steps", []):
            if step.get("type") == "model_output":
                texts.extend(part.get("text", "") for part in step.get("content", []) if part.get("type") == "text")
        if not texts and interaction.get("output_text"):
            texts = [interaction["output_text"]]
        if not texts:
            raise RuntimeError("Gemini가 원고를 반환하지 않았습니다. 선택한 모델의 검색·JSON 출력 권한을 확인하세요.")
        return json.loads(texts[-1])

    # Claude's cited web-search response cannot be combined with JSON schema
    # output in one request, so research and formatting are separate calls.
    headers = {"x-api-key": key, "anthropic-version": "2026-10-21"}
    research = post_json("Claude", "https://api.anthropic.com/v1/messages", headers, {
        "model": model, "max_tokens": 4000, "system": instructions,
        "messages": [{"role": "user", "content": prompt + "\n\n먼저 최신 공식 자료를 검색하고, 확인한 사실과 실제 URL을 간결하게 정리하세요."}],
        "tools": [{"type": "web_search_20250305", "name": "web_search", "max_uses": 4}],
    })
    research_text = "\n".join(block.get("text", "") for block in research.get("content", [])
                              if block.get("type") == "text")
    if not research_text:
        raise RuntimeError("Claude 검색 결과를 받지 못했습니다.")
    formatted = post_json("Claude", "https://api.anthropic.com/v1/messages", headers, {
        "model": model, "max_tokens": 12000, "system": instructions,
        "messages": [{"role": "user", "content": prompt + "\n\n확인한 자료:\n" + research_text[:30000]
                      + "\n\n위 자료에 실제로 있는 출처 URL만 사용하여 완성하세요."}],
        "output_config": {"format": {"type": "json_schema", "schema": schema}},
    })
    text = "".join(block.get("text", "") for block in formatted.get("content", [])
                   if block.get("type") == "text")
    if not text:
        raise RuntimeError("Claude가 원고를 반환하지 않았습니다.")
    return json.loads(text)


# Keep the existing planning, article, revision, and rendering logic.
agent.call_openai = call_provider


class HostedHandler(agent.Handler):
    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path == "/api/status":
            return self.send_json(200, {"hosted": True, "providers": MODELS})
        routes = {"/": (ROOT / "web-static" / "index.html", "text/html; charset=utf-8"),
                  "/app.js": (ROOT / "web-static" / "app.js", "text/javascript; charset=utf-8"),
                  "/style.css": (ROOT / "static" / "style.css", "text/css; charset=utf-8"),
                  "/favicon.svg": (ROOT / "static" / "favicon.svg", "image/svg+xml")}
        if path not in routes:
            return self.send_json(404, {"error": "페이지를 찾을 수 없습니다."})
        file, mime = routes[path]
        body = file.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'")
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        if self.path not in {"/api/generate", "/api/demo"}:
            return self.send_json(404, {"error": "기능을 찾을 수 없습니다."})
        public_origin = os.getenv("PUBLIC_ORIGIN") or os.getenv("RENDER_EXTERNAL_URL", "")
        origin = self.headers.get("Origin", "")
        if public_origin and origin != public_origin.rstrip("/"):
            return self.send_json(403, {"error": "공개 웹앱 주소에서 다시 시도해 주세요."})
        credential_token = None
        try:
            size = int(self.headers.get("Content-Length", "0"))
            if size < 1 or size > agent.MAX_REQUEST_BYTES:
                raise ValueError("입력 크기는 60KB 이하여야 합니다.")
            raw = json.loads(self.rfile.read(size).decode("utf-8"))
            demo = self.path == "/api/demo"
            if not demo:
                provider = str(raw.pop("provider", ""))
                key = str(raw.pop("api_key", "")).strip()
                model = str(raw.pop("model", ""))
                if provider not in MODELS or model not in MODELS[provider]:
                    raise ValueError("서비스와 모델을 선택해 주세요.")
                if not key or len(key) > 512 or "\n" in key or "\r" in key or key.startswith("key_"):
                    raise ValueError("해당 서비스에서 발급한 비밀 API 키를 입력해 주세요. 키 ID는 사용할 수 없습니다.")
                credential_token = CURRENT_CREDENTIAL.set((provider, key, model))
            data = agent.clean_input(raw)
            if data["mode"] == "full":
                result, page = agent.generate_full(data, demo)
            else:
                result = (agent.generate_article(data, demo) if data["mode"] == "article" else
                          agent.demo_data("plan", data["tone"]) if demo else call_provider(data))
                page = agent.render(result, data["mode"], demo)
            return self.send_json(200, {"result": result, "html": page, "demo": demo,
                                        "mode": data["mode"], "tone": data["tone"]})
        except (ValueError, RuntimeError, json.JSONDecodeError) as exc:
            return self.send_json(400, {"error": str(exc)})
        except Exception:
            return self.send_json(500, {"error": "요청 처리에 실패했습니다. 잠시 후 다시 시도해 주세요."})
        finally:
            if credential_token is not None:
                CURRENT_CREDENTIAL.reset(credential_token)


def main():
    port = int(os.getenv("PORT", "8765"))
    if not os.getenv("PUBLIC_ORIGIN") and not os.getenv("RENDER_EXTERNAL_URL"):
        print("PUBLIC_ORIGIN 또는 RENDER_EXTERNAL_URL을 설정하세요. HTTPS 배포 환경에서만 사용합니다.")
        raise SystemExit(1)
    print(f"블로그 에이전트 웹 서버 시작: 포트 {port}")
    ThreadingHTTPServer(("0.0.0.0", port), HostedHandler).serve_forever()


if __name__ == "__main__":
    main()

