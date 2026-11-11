"""Local Sign in with ChatGPT OAuth session for the portable blog app."""
from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

import jwt

AUTH = "https://auth.openai.com"
RESOURCE = "https://api.openai.com/v1"
SCOPES = "openid profile email offline_access resource.invoke chatgpt.tokens.use.direct"
STORE = Path(os.getenv("APPDATA") or Path.home() / ".config") / "modulabs-blog-agent" / "chatgpt-session.json"
LOCK = threading.RLock()
PENDING: dict[str, dict] = {}


def _read() -> dict:
    if not STORE.exists():
        return {"host_id": "urn:uuid:" + str(uuid.uuid4()), "accounts": {}, "active": "", "model": ""}
    return json.loads(STORE.read_text(encoding="utf-8"))


def _write(data: dict) -> None:
    STORE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STORE.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as out:
        json.dump(data, out, ensure_ascii=False)
    if os.name != "nt":
        os.chmod(tmp, 0o600)
    os.replace(tmp, STORE)


def _request_json(url: str, *, form: dict | None = None, token: str = "") -> dict:
    body = urllib.parse.urlencode(form).encode() if form is not None else None
    headers = {"Accept": "application/json"}
    if form is not None:
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    if token:
        headers["Authorization"] = "Bearer " + token
    req = urllib.request.Request(url, data=body, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=30) as response:
            content = response.read()
            return json.loads(content) if content else {}
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:300]
        raise RuntimeError(f"ChatGPT 인증 요청 실패 ({exc.code}): {detail}") from exc


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def start(callback_uri: str, client_id: str = "") -> str:
    with LOCK:
        saved = _read()
        if not STORE.exists():
            _write(saved)
        if client_id and client_id not in saved["accounts"]:
            raise ValueError("저장된 계정을 찾을 수 없습니다.")
        state = secrets.token_urlsafe(32)
        nonce = secrets.token_urlsafe(32)
        verifier = secrets.token_urlsafe(64)
        PENDING[state] = {"nonce": nonce, "verifier": verifier, "callback_uri": callback_uri,
                          "client_id": client_id, "created": time.time()}
        params = {
            "client_id": client_id or "dynamic_agent_client",
            "ext_agent_host_id": saved["host_id"],
            "response_type": "code", "redirect_uri": callback_uri,
            "scope": SCOPES, "resource": RESOURCE,
            "state": state, "nonce": nonce,
            "code_challenge_method": "S256",
            "code_challenge": _b64url(hashlib.sha256(verifier.encode()).digest()),
        }
        if not client_id:
            params["agent_name_hint"] = "모두의연구소 블로그 콘텐츠 에이전트"
        else:
            account = saved["accounts"][client_id]
            if account.get("id_token"):
                params["id_token_hint"] = account["id_token"]
            if account.get("email"):
                params["login_hint"] = account["email"]
        return AUTH + "/api/accounts/authorize?" + urllib.parse.urlencode(params)


def complete(query: dict) -> str:
    state = query.get("state", [""])[0]
    with LOCK:
        pending = PENDING.pop(state, None)
        if not pending or time.time() - pending["created"] > 600:
            raise ValueError("로그인 요청이 만료되었거나 확인되지 않았습니다. 다시 시도해 주세요.")
        if query.get("error"):
            raise ValueError("ChatGPT 로그인이 취소되었거나 권한이 허용되지 않았습니다.")
        code = query.get("code", [""])[0]
        issued = query.get("client_id", [""])[0]
        client_id = pending["client_id"] or issued
        if not code or not client_id or client_id == "dynamic_agent_client":
            raise ValueError("ChatGPT 등록 결과가 불완전합니다. 다시 로그인해 주세요.")
        if pending["client_id"] and issued and issued != pending["client_id"]:
            raise ValueError("로그인 계정 정보가 예상과 다릅니다.")
        tokens = _request_json(AUTH + "/api/accounts/oauth/token", form={
            "grant_type": "authorization_code", "client_id": client_id, "code": code,
            "code_verifier": pending["verifier"], "redirect_uri": pending["callback_uri"],
            "resource": RESOURCE,
        })
        identity = validate_id_token(tokens.get("id_token", ""), client_id, pending["nonce"])
        scopes = tokens.get("scope", "").split()
        if "chatgpt.tokens.use.direct" not in scopes:
            raise ValueError("이 계정에서 ChatGPT 요금제 사용 권한이 허용되지 않았습니다.")
        saved = _read()
        previous = saved["accounts"].get(client_id)
        if previous and previous["subject"] != identity["sub"]:
            raise ValueError("기존에 저장한 계정과 다른 계정이 선택되었습니다.")
        saved["accounts"][client_id] = {
            "client_id": client_id, "subject": identity["sub"],
            "email": identity.get("email", ""), "id_token": tokens["id_token"],
            "access_token": tokens["access_token"], "refresh_token": tokens.get("refresh_token", ""),
            "expires_at": time.time() + int(tokens.get("expires_in", 3600)), "scopes": scopes,
        }
        saved["active"] = client_id
        saved["model"] = ""
        _write(saved)
        return identity.get("email", "ChatGPT 계정")


def validate_id_token(token: str, client_id: str, nonce: str) -> dict:
    if not token:
        raise ValueError("ID 토큰이 없어 계정을 확인할 수 없습니다.")
    config = _request_json(AUTH + "/.well-known/openid-configuration")
    jwks_uri = config.get("jwks_uri", "")
    if not jwks_uri.startswith(AUTH + "/"):
        raise RuntimeError("인증 서버의 서명키 주소를 확인할 수 없습니다.")
    signing_key = jwt.PyJWKClient(jwks_uri).get_signing_key_from_jwt(token)
    identity = jwt.decode(token, signing_key.key, algorithms=["RS256"], audience=client_id,
                          issuer=AUTH, options={"require": ["exp", "iss", "aud", "sub"]})
    if identity.get("nonce") != nonce:
        raise ValueError("로그인 확인값이 일치하지 않습니다.")
    return identity


def access_token() -> str:
    with LOCK:
        saved = _read()
        account = saved["accounts"].get(saved["active"])
        if not account:
            raise RuntimeError("먼저 ChatGPT로 로그인해 주세요.")
        if account["expires_at"] > time.time() + 90:
            return account["access_token"]
        if not account.get("refresh_token"):
            raise RuntimeError("로그인 기간이 만료되었습니다. ChatGPT로 다시 로그인해 주세요.")
        tokens = _request_json(AUTH + "/api/accounts/oauth/token", form={
            "grant_type": "refresh_token", "client_id": account["client_id"],
            "refresh_token": account["refresh_token"], "resource": RESOURCE,
        })
        account["access_token"] = tokens["access_token"]
        account["refresh_token"] = tokens.get("refresh_token", account["refresh_token"])
        account["expires_at"] = time.time() + int(tokens.get("expires_in", 3600))
        account["scopes"] = tokens.get("scope", " ".join(account["scopes"])).split()
        if "chatgpt.tokens.use.direct" not in account["scopes"]:
            raise RuntimeError("ChatGPT 요금제 사용 권한이 해제되었습니다. 다시 로그인해 주세요.")
        _write(saved)
        return account["access_token"]


def models() -> list[dict]:
    token = access_token()
    catalog = _request_json(RESOURCE + "/models", token=token)
    return [{"slug": item["slug"], "display_name": item.get("display_name", item["slug"])}
            for item in catalog.get("models", []) if item.get("visibility") == "list" and item.get("slug")]


def select_model(slug: str) -> None:
    if slug not in {item["slug"] for item in models()}:
        raise ValueError("이 계정에서 사용할 수 없는 모델입니다.")
    with LOCK:
        saved = _read()
        saved["model"] = slug
        _write(saved)


def status() -> dict:
    with LOCK:
        saved = _read()
        account = saved["accounts"].get(saved["active"])
        return {"signed_in": bool(account), "email": account.get("email", "") if account else "",
                "client_id": saved["active"], "model": saved.get("model", ""),
                "accounts": [{"client_id": key, "email": value.get("email", "")}
                             for key, value in saved["accounts"].items()]}


def sign_out() -> None:
    with LOCK:
        saved = _read()
        account = saved["accounts"].get(saved["active"])
        if account and account.get("refresh_token"):
            try:
                config = _request_json(AUTH + "/.well-known/openid-configuration")
                endpoint = config.get("revocation_endpoint", "")
                if endpoint.startswith(AUTH + "/"):
                    _request_json(endpoint, form={"token": account["refresh_token"],
                                                  "token_type_hint": "refresh_token",
                                                  "client_id": account["client_id"]})
            except (RuntimeError, urllib.error.URLError):
                pass
        if account:
            for field in ("access_token", "refresh_token", "id_token"):
                account[field] = ""
            account["expires_at"] = 0
        saved["active"] = ""
        saved["model"] = ""
        _write(saved)
