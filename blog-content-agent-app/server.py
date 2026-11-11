"""Portable local web app for the Blog Content Writer agent.

Requires Python 3.10+. Live generation uses the OpenAI Responses API from the
local Python process. The API key never appears in the browser source.
"""

from __future__ import annotations

import html
import json
import os
import re
import threading
import urllib.error
import urllib.request
import webbrowser
from datetime import date
from html.parser import HTMLParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import markdown
import chatgpt_auth


ROOT = Path(__file__).resolve().parent
STATIC = ROOT / "static"
MAX_REQUEST_BYTES = 60_000


def load_env() -> None:
    config = ROOT / ".env"
    if not config.exists():
        return
    for line in config.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        if key.strip() in {"OPENAI_API_KEY", "OPENAI_MODEL"} and key.strip() not in os.environ:
            os.environ[key.strip()] = value.strip().strip('"').strip("'")


load_env()
MODEL = os.getenv("OPENAI_MODEL", "gpt-6-astra")
BASE_INSTRUCTIONS = (ROOT / "agent-instructions.md").read_text(encoding="utf-8")

TONE_CONTRACTS = {
    "A": """톤 A · 직접 찾아본 정보형
검색자의 고민에서 자연스럽게 출발해 실제 확인 기준을 먼저 설명하세요. 개인 블로그처럼
`여기서 헷갈리기 쉬운 건`, `제가 정리한 순서는`처럼 편하게 말하되 안내문처럼 쓰지 마세요.
질문 → 답 → 근거·예시 → 바로 확인할 일의 흐름을 사용하세요. 이모지는 상황에 맞게
본문 전반에 몇 곳 쓰되 ^^는 쓰지 마세요. 실제 Markdown 비교표/일정표 1개 이상,
번호 목록 또는 체크리스트 1개 이상을 넣고, 표 앞뒤에 해설을 붙이세요.
""",
    "B": """톤 B · 친한 친구처럼 이야기하는 모두연 현장형
독자가 겪는 장면이나 실제 확인한 작은 발견에서 시작하세요. 친구에게 말하듯
짧은 반응, 말맛 있는 문장, 질문을 섞으세요. `ㅠㅠㅠ`, `!!!`, `ㅋㅋ`, `ㅎㅎ`,
🙂📌📝🔍💡 같은 이모지·표현을 글의 감정이 생기는 곳에 여러 번 자연스럽게 사용하세요.
^^는 쓰지 마세요. 시도/확인 → 막힌 점·의외의 결과 → 수정·해결 → 독자의 다음 행동을
따라가되 모든 소제목을 같은 틀로 반복하지 마세요. 중간중간 실제 Markdown 표 1개 이상,
번호 목록·체크리스트 1개 이상, 짧은 캡션을 넣으세요. 사용자나 작성자가 실제로 하지
않은 경험·대화·성과를 지어내지 마세요. 이모지로 정보 부족을 덮지 마세요.
""",
}


def obj(properties: dict) -> dict:
    return {"type": "object", "properties": properties,
            "required": list(properties), "additionalProperties": False}


STRING = {"type": "string"}
STRINGS = {"type": "array", "items": STRING}
PLAN_SCHEMA = obj({
    "topics": {"type": "array", "items": obj({
        "topic": STRING, "title": STRING, "seo_tags": STRINGS,
        "search_questions": STRING, "draft": STRING, "images": STRING,
        "metrics": STRING, "reader_benefit": STRING, "traffic": STRING,
    })},
    "common_sources": {"type": "array", "items": obj({"title": STRING, "url": STRING})},
    "method_note": STRING,
})
ARTICLE_SCHEMA = obj({
    "title": STRING, "description": STRING, "body_markdown": STRING,
    "seo_tags": STRINGS, "image_briefs": STRINGS,
    "sources": {"type": "array", "items": obj({"title": STRING, "url": STRING})},
    "publication_checks": STRINGS,
})


def clean_input(payload: dict) -> dict:
    if not isinstance(payload, dict):
        raise ValueError("입력 형식이 올바르지 않습니다.")
    allowed = {"mode", "tone", "target", "purpose", "audience", "course_url",
               "topic", "topic_count", "evidence", "notes"}
    data = {}
    for key in allowed:
        value = payload.get(key, "")
        if key == "topic_count":
            try:
                value = max(1, min(10, int(value)))
            except (ValueError, TypeError):
                value = 10
        else:
            limit = 12_000 if key == "evidence" else 5_000 if key == "course_url" else 1_000
            value = str(value).strip()[:limit]
        data[key] = value
    if data["mode"] not in {"full", "plan", "article"}:
        raise ValueError("작업 유형을 선택해 주세요.")
    if data["tone"] not in {"A", "B"}:
        raise ValueError("톤앤매너를 선택해 주세요.")
    if not data["target"] or not data["purpose"]:
        raise ValueError("홍보 대상과 목적을 입력해 주세요.")
    if data["mode"] == "article" and not data["topic"]:
        raise ValueError("원고를 작성할 주제를 입력해 주세요.")
    course_urls = []
    for line in data["course_url"].splitlines():
        url = line.strip()
        if not url:
            continue
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc or any(ch.isspace() for ch in url):
            raise ValueError("공식 과정 URL은 한 줄에 http 또는 https 주소 하나씩 입력해 주세요.")
        if url not in course_urls:
            course_urls.append(url)
    data["course_urls"] = course_urls
    return data


def build_prompt(data: dict) -> str:
    tone = ("A. 직접 찾아본 정보형" if data["tone"] == "A"
            else "B. 친한 친구처럼 이야기하는 모두연 현장형")
    common = f"""오늘 날짜: {date.today().isoformat()}
작업: {'주제별 기획표' if data['mode'] == 'plan' else '실제 블로그 글'}
홍보 대상: {data['target']}
목적: {data['purpose']}
우선 독자: {data['audience'] or '사용자가 지정하지 않음'}
공식 과정 URL 후보 (사용자 제공): {chr(10).join(f'{i}. {url}' for i, url in enumerate(data['course_urls'], 1)) or '사용자가 지정하지 않음'}
선택 톤: {tone}
추가 조건: {data['notes'] or '없음'}
제공된 검색량·실적 근거: {data['evidence'] or '없음'}

공식 URL과 조건은 가능하면 웹 검색으로 확인하고, 확인 못 한 항목은 미확인이라고 적으세요.
과정 URL이 여러 개면 각각의 실제 커리큘럼과 대상자를 구분해 확인하세요.
주제에 맞는 과정만 자연스럽게 소개하고, 관계없는 과정까지 모두 나열하지 마세요.
네이버·구글 월간 검색량, CTR, 방문자 수를 입력 근거나 실제 조회 없이 지어내지 마세요.
출처 URL은 실제로 확인한 것만 적으세요. 참고 블로그의 문장과 이미지는 복사하지 마세요.
"""
    if data["mode"] == "plan":
        return common + f"""
서로 다른 주제 {data['topic_count']}개를 만드세요. 각 주제에는 독자의 검색 질문, 유용한 정보,
실행 가능한 글 초안, 필요한 이미지, 객관 수치·근거, 독자 효과, 예상 유입율·유입량을 넣으세요.
seo_tags는 각 주제마다 중복 없는 8~10개를 넣으세요. 자료가 없으면 수치 관련 칸에
'미확인' 또는 '미산정'과 이유를 명시하세요. JSON 구조에 맞게 답하세요.
"""
    plan_context = data.get("plan_context", "")
    revision_context = data.get("revision_context", "")
    return common + TONE_CONTRACTS[data["tone"]] + f"""
원고 주제: {data['topic']}
앞 단계에서 만든 기획표의 선택 주제와 근거: {plan_context or '별도 기획표 없음'}
{revision_context}
기획표의 검색 질문과 초안에 답하되, 검증되지 않은 수치는 그대로 사실처럼 옮기지 마세요.
검색자가 가장 궁금해할 질문에 충분히 답하는 완성 원고를 쓰세요. 직접 따라 할 예시,
주의할 점, 공식 근거, 자연스러운 과정 소개를 포함하세요. body_markdown은 독립된
네이버 블로그 원고로 읽히는 Markdown이며 제목(h1)은 제외하세요. 소제목(h2)을 쓰고,
**실제 Markdown 표(머리행과 |---| 구분행), 번호 목록 또는 체크리스트**를 반드시
body_markdown 안에 넣으세요. 줄글이 세 문단 이상 연속되지 않게 표·목록·짧은 문단을
리듬 있게 섞되 장식용 표는 만들지 마세요. 구체적인 작업 순서와 오류 사례를 담으세요.
seo_tags는 8~10개,
image_briefs는 본문에 넣을 이미지의 역할과 내용 2~4개로 작성하세요.
이미지가 유용한 본문 위치에 [[IMAGE_1]], [[IMAGE_2]]처럼 image_briefs의 순번을
한 줄씩 넣으세요. 실제 이미지가 없으므로 독자가 채울 수 있는 안내 카드로 표시됩니다.
실제로 하지 않은 수강, 인터뷰, 상담, 캡처, 실습을 1인칭 경험으로 쓰지 마세요.
출력 전에 톤 {data['tone']}의 문장과 반응이 A/B 중 어느 쪽인지 읽기만 해도
구별되는지, ^^가 없는지, 표와 목록이 실제로 들어갔는지 자체 점검하세요.
JSON 구조에 맞게 답하세요.
"""


def article_style_gaps(article: dict, tone: str) -> list[str]:
    body = str(article.get("body_markdown", ""))
    gaps = []
    if not re.search(r"(?m)^\|[^\n]+\|\s*\n\|\s*:?-{3,}", body):
        gaps.append("본문에 읽을 수 있는 Markdown 표가 없음")
    if not re.search(r"(?m)^\s*(?:[-*] |\d+\. )", body):
        gaps.append("본문에 목록 또는 체크리스트가 없음")
    if "^^" in body:
        gaps.append("요청과 다른 ^^ 표현이 남아 있음")
    if tone == "B":
        reactions = re.findall(r"ㅠ{2,}|!{2,}|ㅋ{2,}|ㅎ{2,}|[\U0001F300-\U0001FAFF]", body)
        if len(reactions) < 2:
            gaps.append("친구처럼 말하는 반응·이모지가 거의 없음")
    return gaps


def extract_text(response: dict) -> str:
    for item in response.get("output", []):
        if item.get("type") == "message":
            for content in item.get("content", []):
                if content.get("type") == "output_text":
                    return content.get("text", "")
    raise RuntimeError("모델이 원고를 반환하지 않았습니다. 모델·도구 권한을 확인해 주세요.")


def call_openai(data: dict) -> dict:
    auth = chatgpt_auth.status()
    via_chatgpt = auth["signed_in"]
    key = chatgpt_auth.access_token() if via_chatgpt else os.getenv("OPENAI_API_KEY", "").strip()
    if not key:
        raise RuntimeError("ChatGPT로 로그인하거나 .env에 API 키를 설정해 주세요.")
    if not via_chatgpt and key.startswith("key_"):
        raise RuntimeError("key_로 시작하는 값은 비밀 API 키가 아닌 키 식별자로 보입니다. API Keys에서 새 비밀 키를 만들고 생성 직후 표시되는 전체 값을 .env의 OPENAI_API_KEY에 넣어주세요.")
    model = auth["model"] if via_chatgpt else MODEL
    if not model:
        raise RuntimeError("ChatGPT 로그인 후 사용할 모델을 선택해 주세요.")
    schema = PLAN_SCHEMA if data["mode"] == "plan" else ARTICLE_SCHEMA
    request_body = {
        "model": model,
        "instructions": BASE_INSTRUCTIONS + "\n반환 형식은 지정된 JSON 스키마를 따르세요.",
        "input": [{"role": "user", "content": build_prompt(data)}] if via_chatgpt else build_prompt(data),
        "tools": [{"type": "web_search"}],
        "text": {"format": {"type": "json_schema", "name": "blog_plan" if data["mode"] == "plan" else "blog_article",
                            "strict": True, "schema": schema}},
        "store": False,
    }
    if via_chatgpt:
        request_body["stream"] = True
    req = urllib.request.Request(
        "https://api.openai.com/v1/responses",
        data=json.dumps(request_body, ensure_ascii=False).encode("utf-8"),
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=240) as stream:
            if via_chatgpt:
                response = read_response_stream(stream)
            else:
                response = json.load(stream)
    except urllib.error.HTTPError as exc:
        try:
            detail = json.loads(exc.read().decode("utf-8", "replace"))
            message = detail.get("error", {}).get("message", str(exc))
        except (ValueError, AttributeError):
            message = str(exc)
        if exc.code == 401 and via_chatgpt:
            raise RuntimeError("ChatGPT 로그인 세션이 만료되었습니다. 다시 로그인해 주세요.") from exc
        if exc.code == 401:
            raise RuntimeError("OpenAI 인증 오류(401): .env의 OPENAI_API_KEY에 실제 비밀 키가 들어 있는지 확인하세요. key_ 식별자나 추적 ID로는 접속할 수 없습니다. 새 비밀 키를 만든 뒤 앱을 다시 실행해 주세요.") from exc
        raise RuntimeError(f"OpenAI API 오류 ({exc.code}): {message[:350]}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"네트워크 연결에 실패했습니다: {exc.reason}") from exc
    if response.get("status") == "incomplete":
        raise RuntimeError("생성이 완료되지 않았습니다. 요청을 줄이거나 다시 시도해 주세요.")
    return json.loads(extract_text(response))


def read_response_stream(stream) -> dict:
    """Consume every SSE event and require the terminal response.completed event."""
    lines = []
    for raw in stream:
        line = raw.decode("utf-8", "replace").rstrip("\r\n")
        if line:
            if line.startswith("data:"):
                lines.append(line[5:].lstrip())
            continue
        if not lines:
            continue
        payload = "\n".join(lines)
        lines = []
        if payload == "[DONE]":
            break
        try:
            event = json.loads(payload)
        except json.JSONDecodeError:
            continue
        kind = event.get("type")
        if kind == "response.completed":
            return event["response"]
        if kind == "response.failed":
            detail = event.get("response", {}).get("error") or {}
            raise RuntimeError("생성이 실패했습니다: " + str(detail.get("message") or detail.get("code") or "알 수 없는 오류"))
        if kind == "response.incomplete":
            raise RuntimeError("생성이 완료되지 않았습니다. 입력을 줄이거나 다시 시도해 주세요.")
        if kind == "error":
            detail = event.get("error") or {}
            raise RuntimeError("생성 오류: " + str(detail.get("message") or detail.get("code") or "알 수 없는 오류"))
    raise RuntimeError("생성 연결이 완료되기 전에 종료되었습니다. 다시 시도해 주세요.")


class SafeHTML(HTMLParser):
    TAGS = {"p", "h2", "h3", "h4", "ul", "ol", "li", "strong", "em", "blockquote",
            "pre", "code", "table", "thead", "tbody", "tr", "th", "td", "br", "hr"}
    VOID = {"br", "hr"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.link_closers = []

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            href = dict(attrs).get("href", "")
            parsed = urlparse(href)
            if parsed.scheme in {"http", "https"} and parsed.netloc:
                self.parts.append(f'<a href="{html.escape(href, quote=True)}" rel="noopener noreferrer" target="_blank">')
                self.link_closers.append("</a>")
            else:
                self.parts.append("<span>")
                self.link_closers.append("</span>")
        elif tag in self.TAGS:
            self.parts.append(f"<{tag}>")

    def handle_endtag(self, tag):
        if tag == "a":
            self.parts.append(self.link_closers.pop() if self.link_closers else "")
        elif tag in self.TAGS and tag not in self.VOID:
            self.parts.append(f"</{tag}>")

    def handle_data(self, data):
        self.parts.append(html.escape(data))

    def get(self):
        return "".join(self.parts)


def md_html(value: str) -> str:
    parser = SafeHTML()
    parser.feed(markdown.markdown(html.escape(value), extensions=["tables", "fenced_code"]))
    return parser.get()


PREVIEW_CSS = """
body{max-width:780px;margin:0 auto;padding:48px 34px;color:#25313a;font:16px/1.85 'Malgun Gothic',Arial,sans-serif;word-break:keep-all}
h1{font-size:30px;line-height:1.4}h2{font-size:23px;margin-top:48px;border-bottom:2px solid #203f3b;padding-bottom:8px}
h3{font-size:18px;margin-top:30px}p{margin:16px 0}a{color:#166b78}table{border-collapse:collapse;width:100%;margin:24px 0;font-size:14px}
th,td{border:1px solid #dce5e4;padding:10px;vertical-align:top;text-align:left}th{background:#eaf5f1}
blockquote{background:#edf7f3;padding:12px 20px;border-left:4px solid #2d8074;margin:24px 0}
pre{overflow:auto;padding:18px;background:#27343e;color:#fff;border-radius:7px}code{font-family:Consolas,monospace}
.note{padding:20px;background:#fff7e8;border-left:4px solid #dfa645;margin:30px 0}.image-brief{margin:28px 0;padding:24px;border:1px dashed #8bbcb2;background:#f5fbf9;border-radius:10px}
.image-brief small,.meta{color:#65737c}.tags{border-top:1px solid #dce5e4;margin-top:44px;padding-top:20px;color:#49655f}
@media(max-width:650px){body{padding:26px 18px}h1{font-size:25px}h2{font-size:21px}table{display:block;overflow:auto}}
"""


def safe_url(value: str) -> str:
    parsed = urlparse(value)
    return value if parsed.scheme in {"http", "https"} and parsed.netloc else ""


def source_html(sources: list) -> str:
    items = []
    for entry in sources:
        if not isinstance(entry, dict):
            continue
        title = html.escape(str(entry.get("title", "출처")))
        url = safe_url(str(entry.get("url", "")))
        items.append(f'<li><a href="{html.escape(url, quote=True)}">{title}</a></li>' if url else f"<li>{title} (URL 미확인)</li>")
    return "<h2>확인한 자료</h2><ul>" + "".join(items) + "</ul>" if items else ""


def render(data: dict, mode: str, demo: bool = False) -> str:
    badge = '<div class="note"><strong>데모 예시:</strong> 입력값으로 생성한 결과가 아닙니다.</div>' if demo else ""
    if mode == "plan":
        blocks = []
        labels = [("글 주제", "topic"), ("글 제목", "title"), ("SEO 태그", "seo_tags"),
                  ("검색자의 질문", "search_questions"), ("글 초안", "draft"),
                  ("필요한 이미지", "images"), ("객관 수치·근거", "metrics"),
                  ("독자 효과", "reader_benefit"), ("예상 유입율·유입량", "traffic")]
        for i, topic in enumerate(data.get("topics", []), 1):
            rows = [f'<tr><th></th><td><strong>{i:02d}. {html.escape(str(topic.get("topic", "")))}</strong></td></tr>']
            for label, key in labels:
                value = topic.get(key, "")
                if isinstance(value, list):
                    value = ", ".join(str(x) for x in value)
                rows.append(f"<tr><th>{label}</th><td>{html.escape(str(value))}</td></tr>")
            blocks.append("<table>" + "".join(rows) + "</table>")
        main = badge + "<h1>블로그 주제별 기획표</h1>" + "".join(blocks)
        main += source_html(data.get("common_sources", []))
        main += f'<div class="note">{html.escape(str(data.get("method_note", "")))}</div>'
    else:
        main = badge + f'<h1>{html.escape(str(data.get("title", "")))}</h1>'
        body_html = md_html(str(data.get("body_markdown", "")))
        used_images = set()
        def image_at_marker(match):
            number = int(match.group(1))
            briefs = data.get("image_briefs", [])
            if number < 1 or number > len(briefs):
                return match.group(0)
            used_images.add(number)
            return (f'<div class="image-brief"><small>이미지 {number} 삽입 위치</small>'
                    f'<p>{html.escape(str(briefs[number-1]))}</p></div>')
        body_html = re.sub(r'<p>\[\[IMAGE_(\d+)\]\]</p>', image_at_marker, body_html)
        main += body_html
        for i, brief in enumerate(data.get("image_briefs", []), 1):
            if i not in used_images:
                main += f'<div class="image-brief"><small>이미지 {i} 삽입 위치</small><p>{html.escape(str(brief))}</p></div>'
        main += source_html(data.get("sources", []))
        tags = " ".join("#" + str(tag).lstrip("#") for tag in data.get("seo_tags", []))
        main += f'<div class="tags">{html.escape(tags)}</div>'
        checks = data.get("publication_checks", [])
        if checks:
            main += '<div class="note"><strong>게시 전 확인</strong><ul>'
            main += "".join(f"<li>{html.escape(str(item))}</li>" for item in checks)
            main += "</ul></div>"
    return ("<!doctype html><html lang=\"ko\"><head><meta charset=\"utf-8\">"
            "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
            "<meta http-equiv=\"Content-Security-Policy\" content=\"default-src 'none'; style-src 'unsafe-inline'; img-src data: https:; connect-src 'none'\">"
            f"<title>{html.escape(str(data.get('title', '블로그 원고')))}</title><style>{PREVIEW_CSS}</style>"
            f"</head><body>{main}</body></html>")


def render_full(plan: dict, article: dict, demo: bool = False) -> str:
    plan_page = render(plan, "plan", demo)
    article_page = render(article, "article", demo)
    plan_body = plan_page.split("<body>", 1)[1].rsplit("</body>", 1)[0]
    article_body = article_page.split("<body>", 1)[1].rsplit("</body>", 1)[0]
    combined = ("<nav><a href='#plan'>① 주제별 기획표</a> · "
                "<a href='#article'>② 첫 번째 주제의 블로그 원고</a></nav>"
                f"<section id='plan'>{plan_body}</section><hr>"
                f"<section id='article'>{article_body}</section>")
    return plan_page.replace(f"<body>{plan_body}</body>", f"<body>{combined}</body>", 1)


def generate_article(data: dict, demo: bool = False) -> dict:
    if demo:
        return demo_data("article", data["tone"])
    article = call_openai(data)
    gaps = article_style_gaps(article, data["tone"])
    if gaps:
        revision = {**data, "revision_context": (
            "다음 초안은 내용·출처는 유지하되 문체와 구성을 수정할 대상입니다. "
            "부족한 점: " + "; ".join(gaps) + "\n초안 JSON: "
            + json.dumps(article, ensure_ascii=False)[:24_000] + "\n"
            "같은 JSON 구조로 전체 원고를 다시 반환하세요. 사실·URL을 새로 지어내지 마세요."
        )}
        article = call_openai(revision)
        remaining = article_style_gaps(article, data["tone"])
        if remaining:
            checks = article.get("publication_checks")
            if isinstance(checks, list):
                checks.append("톤·표·목록 확인 필요: " + "; ".join(remaining))
    return article


def generate_full(data: dict, demo: bool = False) -> tuple[dict, str]:
    plan_input = {**data, "mode": "plan"}
    plan = demo_data("plan", data["tone"]) if demo else call_openai(plan_input)
    topics = plan.get("topics", [])
    if not isinstance(topics, list) or not topics or not isinstance(topics[0], dict):
        raise RuntimeError("기획표에 주제가 없어 원고를 이어서 작성할 수 없습니다. 다시 생성해 주세요.")
    chosen = topics[0]
    selected_topic = str(chosen.get("title") or chosen.get("topic") or "").strip()
    if not selected_topic:
        raise RuntimeError("첫 번째 주제의 제목이 없어 원고를 이어서 작성할 수 없습니다.")
    article_input = {**data, "mode": "article", "topic": selected_topic,
                     "plan_context": json.dumps({"selected_topic": chosen,
                                                 "common_sources": plan.get("common_sources", [])}, ensure_ascii=False)}
    article = generate_article(article_input, demo)
    result = {"plan": plan, "article": article, "selected_topic": selected_topic}
    return result, render_full(plan, article, demo)


def demo_data(mode: str, tone: str) -> dict:
    if mode == "plan":
        return {
            "topics": [{"topic": "부트캠프 준비 수준 점검", "title": "AI 부트캠프 시작 전 무엇을 준비할까?",
                        "seo_tags": ["AI부트캠프", "부트캠프준비", "비전공자AI", "파이썬기초", "AI입문", "부트캠프선택", "데이터분석입문", "AI학습", "모두의연구소"],
                        "search_questions": "지원 전 어느 수준이어야 할까? 무엇부터 연습할까?",
                        "draft": "목표 직무를 한 문장으로 적고, CSV 한 파일을 열어 데이터 구조와 빈 값을 점검한다. 4주간 작은 결과물을 남기는 연습을 제안한다.",
                        "images": "수준 진단표, 4주 로드맵, 예시 결과물",
                        "metrics": "데모에는 실측 검색량이 없으므로 미확인",
                        "reader_benefit": "부족한 지점을 파악하고 첫 주 과제를 정할 수 있다.",
                        "traffic": "검색량·기존 실적이 없어 미산정"}],
            "common_sources": [], "method_note": "고정된 화면 예시입니다. 실제 검색량 조사나 원고 생성은 API 키 설정 후 실행하세요.",
        }
    body = ("""부트캠프 준비 목록을 보면 파이썬, 수학, AI가 한꺼번에 나옵니다. 어디까지 끝내야 지원할 수 있는지부터 헷갈리죠. 😅

제가 이 주제를 정리한다면 **완벽하게 선행학습하기**보다, 지금 설명할 수 있는 것과 막히는 지점을 먼저 확인하겠습니다. 아래 표는 공식 지원 기준이 아니라 스스로 점검할 때 쓰는 예시예요. 📌

## 4주를 이렇게 나눠보세요

| 시기 | 해볼 일 | 남길 결과 |
|---|---|---|
| 1주차 | CSV 열 이름·빈 값 확인 | 메모 3줄 |
| 2주차 | 평균·비율 한 번 계산 | 계산 과정 |
| 3~4주차 | 작은 질문 하나 분석 | 결과 설명 한 장 |

특히 1주차부터 결과물을 남겨두면 다음 주에 무엇을 고칠지 보입니다. 📝

[[IMAGE_1]]

## 오늘 바로 확인할 순서

1. 관심 있는 직무를 한 문장으로 적습니다.
2. 연습용 CSV 한 파일을 열어 열 이름과 빈 값을 표시합니다.
3. 계산 하나를 해보고 **왜 그 결과가 나왔는지** 자기 말로 설명합니다.

설명이 멈추는 부분이 다음 공부 주제입니다. 과정을 비교할 때도 이 빈칸을 채울 수 있는 커리큘럼인지 확인해 보세요. 🔍

[[IMAGE_2]]""" if tone == "A" else
            """부트캠프 준비하려고 검색했는데 파이썬, 수학, AI가 한 화면에 쫙 뜨면요? ‘잠깐... 이걸 다 끝내야 한다고?!’ 싶죠 ㅠㅠㅠ

일단 숨 한번 고르고요 😮‍💨 준비를 **4주짜리 작은 실험**으로 나눠보면 훨씬 덜 막막합니다. 제가 추천하는 시작점은 거창한 모델 만들기가 아니라 CSV 파일 하나예요. 진짜로요!

## 처음부터 전부 공부해야 할까요?

그렇게 시작하면 할 일만 길어져요. 대신 주마다 *눈에 보이는 결과*를 하나씩 남겨보세요. 📝

| 시기 | 해볼 일 | 남길 결과 |
|---|---|---|
| 1주차 | CSV 열 이름·빈 값 찾기 | 발견한 점 3줄 |
| 2주차 | 평균·비율 한 번 계산 | 계산 과정 |
| 3~4주차 | 작은 질문 하나 분석 | 결과 설명 한 장 |

표가 좀 소박하죠? 근데 첫 주에 빈 값 하나만 제대로 찾아도 ‘아, 내가 여기서 막히는구나’가 보입니다. 이게 다음 공부 순서를 정할 힌트예요 💡

[[IMAGE_1]]

## 오늘은 딱 이것만 해봐요!

1. 관심 있는 직무를 한 문장으로 적기
2. 연습용 CSV 하나 열고 열 이름·빈 값 표시하기
3. 평균 하나 계산한 뒤 결과를 친구에게 설명하듯 적기

3번에서 말이 막힌다면요? 괜찮아요!!! 그 부분을 다음 주에 배우면 됩니다. 과정도 그 빈칸을 채워줄 수 있는지 비교해 보세요. 🔍

[[IMAGE_2]]""")
    return {"title": "AI 부트캠프 시작 전 4주 준비 체크리스트 (데모)",
            "description": "에이전트 원고 화면 예시", "body_markdown": body,
            "seo_tags": ["AI부트캠프", "부트캠프준비", "비전공자AI", "파이썬기초", "AI입문", "데이터분석입문", "AI학습", "부트캠프체크리스트", "모두의연구소"],
            "image_briefs": ["4주 준비 로드맵을 직접 제작해 삽입", "CSV 빈 값 확인 화면을 예시로 제작해 삽입"], "sources": [],
            "publication_checks": ["실제 공식 과정 링크와 신청 조건 확인", "데모 문구를 실제 원고로 교체"]}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        # OAuth callback URLs contain one-time codes; never print them.
        path = urlparse(self.path).path
        print(f'{self.address_string()} {self.command} {path}')

    def local_request(self) -> bool:
        host = self.headers.get("Host", "")
        return host in {f"127.0.0.1:{self.server.server_port}", "127.0.0.1"}

    def send_json(self, status: int, payload: dict):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if not self.local_request():
            return self.send_json(403, {"error": "로컬 주소로 접속해 주세요."})
        path = urlparse(self.path).path
        if path == "/auth/callback":
            try:
                email = chatgpt_auth.complete(parse_qs(urlparse(self.path).query))
                message = f"{html.escape(email)} 계정으로 연결되었습니다. 이 창을 닫고 앱으로 돌아가세요."
            except (ValueError, RuntimeError, urllib.error.URLError) as exc:
                message = "로그인에 실패했습니다: " + html.escape(str(exc))
            page = ("<!doctype html><html lang='ko'><meta charset='utf-8'><title>ChatGPT 로그인</title>"
                    "<body style='font:18px sans-serif;max-width:540px;margin:80px auto'>"
                    f"<h1>ChatGPT 로그인</h1><p>{message}</p>"
                    "<script>if(window.opener){window.opener.postMessage('chatgpt-auth-complete',location.origin);window.close()}</script>"
                    "</body></html>")
            body = page.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            return self.wfile.write(body)
        if path == "/api/models":
            try:
                return self.send_json(200, {"models": chatgpt_auth.models()})
            except (RuntimeError, urllib.error.URLError) as exc:
                return self.send_json(400, {"error": str(exc)})
        routes = {"/": ("index.html", "text/html; charset=utf-8"),
                  "/style.css": ("style.css", "text/css; charset=utf-8"),
                  "/app.js": ("app.js", "text/javascript; charset=utf-8"),
                  "/favicon.svg": ("favicon.svg", "image/svg+xml")}
        if path == "/api/status":
            configured_key = os.getenv("OPENAI_API_KEY", "").strip()
            key_state = ("missing" if not configured_key else
                         "looks_like_id" if configured_key.startswith("key_") else "configured_unverified")
            auth = chatgpt_auth.status()
            return self.send_json(200, {"live_ready": (auth["signed_in"] and bool(auth["model"])) or key_state == "configured_unverified",
                                        "key_state": key_state, "model": MODEL, "chatgpt": auth})
        if path not in routes:
            return self.send_json(404, {"error": "페이지를 찾을 수 없습니다."})
        file_name, mime = routes[path]
        body = (STATIC / file_name).read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        if not self.local_request():
            return self.send_json(403, {"error": "로컬 주소로 접속해 주세요."})
        origin = self.headers.get("Origin", "")
        if origin and origin != f"http://127.0.0.1:{self.server.server_port}":
            return self.send_json(403, {"error": "이 앱 화면에서 다시 시도해 주세요."})
        if self.path not in {"/api/generate", "/api/demo", "/api/auth/start", "/api/auth/logout", "/api/model"}:
            return self.send_json(404, {"error": "기능을 찾을 수 없습니다."})
        try:
            size = int(self.headers.get("Content-Length", "0"))
            if size < 0 or size > MAX_REQUEST_BYTES:
                raise ValueError("입력 크기는 60KB 이하여야 합니다.")
            raw = json.loads(self.rfile.read(size).decode("utf-8")) if size else {}
            if self.path == "/api/auth/start":
                callback = f"http://127.0.0.1:{self.server.server_port}/auth/callback"
                return self.send_json(200, {"url": chatgpt_auth.start(callback, str(raw.get("client_id", "")))})
            if self.path == "/api/auth/logout":
                chatgpt_auth.sign_out()
                return self.send_json(200, {"ok": True})
            if self.path == "/api/model":
                chatgpt_auth.select_model(str(raw.get("model", "")))
                return self.send_json(200, {"ok": True})
            data = clean_input(raw)
            demo = self.path == "/api/demo"
            if data["mode"] == "full":
                result, page = generate_full(data, demo)
            else:
                result = (generate_article(data, demo) if data["mode"] == "article" else
                          demo_data("plan", data["tone"]) if demo else call_openai(data))
                page = render(result, data["mode"], demo)
            return self.send_json(200, {"result": result, "html": page, "demo": demo,
                                        "mode": data["mode"], "tone": data["tone"]})
        except (ValueError, RuntimeError, json.JSONDecodeError) as exc:
            return self.send_json(400, {"error": str(exc)})
        except Exception as exc:
            return self.send_json(500, {"error": f"요청 처리에 실패했습니다: {type(exc).__name__}"})


def main():
    host, port = "127.0.0.1", int(os.getenv("BLOG_AGENT_PORT", "8765"))
    url = f"http://{host}:{port}"
    server = ThreadingHTTPServer((host, port), Handler)
    print(f"\n블로그 작성 에이전트: {url}")
    print("ChatGPT로 로그인해 요금제 사용량으로 생성할 수 있습니다. API 키 사용도 선택 가능합니다.")
    print("종료: Ctrl+C\n")
    if os.getenv("NO_BROWSER") != "1":
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
