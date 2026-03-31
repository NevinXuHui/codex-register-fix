"""
OAuth 登录状态机 V2
复用现有注册会话中的 session/email service，仅重做后置 OAuth 登录与授权码提取流程。
"""

from dataclasses import dataclass
import base64
import hashlib
import json
import random
import re
import secrets
import time
import uuid
from typing import Any, Callable, Dict, Optional, Tuple
from urllib.parse import parse_qs, unquote, urlparse

from .session_reuse_v2 import (
    FlowState,
    build_browser_headers,
    describe_flow_state,
    extract_flow_state,
    normalize_flow_url,
)


def _generate_datadog_trace() -> Dict[str, str]:
    trace_id = str(random.getrandbits(64))
    parent_id = str(random.getrandbits(64))
    trace_hex = format(int(trace_id), "016x")
    parent_hex = format(int(parent_id), "016x")
    return {
        "traceparent": f"00-0000000000000000{trace_hex}-{parent_hex}-01",
        "tracestate": "dd=s:1;o:rum",
        "x-datadog-origin": "rum",
        "x-datadog-parent-id": parent_id,
        "x-datadog-sampling-priority": "1",
        "x-datadog-trace-id": trace_id,
    }


def _generate_pkce() -> Tuple[str, str]:
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(64)).rstrip(b"=").decode("ascii")
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return verifier, challenge


def _seed_oai_device_cookie(session, device_id: str) -> None:
    for domain in (
        "chatgpt.com",
        ".chatgpt.com",
        "openai.com",
        ".openai.com",
        "auth.openai.com",
        ".auth.openai.com",
    ):
        try:
            session.cookies.set("oai-did", device_id, domain=domain)
        except Exception:
            continue


@dataclass
class OAuthLoginConfig:
    oauth_issuer: str
    oauth_client_id: str
    oauth_redirect_uri: str
    oauth_scope: str
    user_agent: str
    sec_ch_ua: str
    browser_mode: str = "protocol"
    impersonate: str = "chrome131"


class OAuthLoginV2:
    """0330 风格的 OAuth 登录状态机。"""

    def __init__(
        self,
        *,
        session,
        email: str,
        password: str,
        email_service,
        email_info: Optional[Dict[str, Any]],
        device_id: str,
        config: OAuthLoginConfig,
        logger: Optional[Callable[[str], None]] = None,
        sentinel_builder: Optional[Callable[..., Optional[str]]] = None,
        preferred_workspace_id: str = "",
    ):
        self.session = session
        self.email = email
        self.password = password
        self.email_service = email_service
        self.email_info = email_info or {}
        self.device_id = str(device_id or "").strip()
        self.config = config
        self.logger = logger
        self.sentinel_builder = sentinel_builder
        self.preferred_workspace_id = str(preferred_workspace_id or "").strip()
        self._last_follow_url: str = ""

    def _log(self, message: str) -> None:
        if self.logger:
            self.logger(message)

    def _browser_pause(self, low: float = 0.15, high: float = 0.4) -> None:
        if self.config.browser_mode == "headed":
            time.sleep(random.uniform(low, high))
        elif self.config.browser_mode == "headless":
            time.sleep(random.uniform(0.05, 0.15))

    def _headers(
        self,
        url: str,
        *,
        accept: str,
        referer: Optional[str] = None,
        origin: Optional[str] = None,
        content_type: Optional[str] = None,
        navigation: bool = False,
        fetch_mode: Optional[str] = None,
        fetch_dest: Optional[str] = None,
        fetch_site: Optional[str] = None,
        extra_headers: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, str]:
        return build_browser_headers(
            url=url,
            user_agent=self.config.user_agent,
            sec_ch_ua=self.config.sec_ch_ua,
            chrome_full_version="131.0.0.0",
            accept=accept,
            accept_language="en-US,en;q=0.9",
            referer=referer,
            origin=origin,
            content_type=content_type,
            navigation=navigation,
            fetch_mode=fetch_mode,
            fetch_dest=fetch_dest,
            fetch_site=fetch_site,
            headed=self.config.browser_mode == "headed",
            extra_headers=extra_headers,
        )

    def _state_from_url(self, url: str, method: str = "GET") -> FlowState:
        state = extract_flow_state(
            current_url=normalize_flow_url(url, auth_base=self.config.oauth_issuer),
            auth_base=self.config.oauth_issuer,
            default_method=method,
        )
        state.method = str(method or "GET").upper()
        return state

    def _state_from_payload(self, data: Dict[str, Any], current_url: str = "") -> FlowState:
        return extract_flow_state(
            data=data,
            current_url=current_url,
            auth_base=self.config.oauth_issuer,
        )

    def _state_signature(self, state: FlowState) -> Tuple[str, str, str, str]:
        return (
            state.page_type or "",
            state.method or "",
            state.continue_url or "",
            state.current_url or "",
        )

    def _extract_code_from_url(self, url: str) -> Optional[str]:
        if not url or "code=" not in url:
            return None
        try:
            return parse_qs(urlparse(url).query).get("code", [None])[0]
        except Exception:
            return None

    def _extract_code_from_state(self, state: FlowState) -> Optional[str]:
        for candidate in (
            state.continue_url,
            state.current_url,
            (state.payload or {}).get("url", ""),
        ):
            code = self._extract_code_from_url(candidate)
            if code:
                return code
        return None

    def _state_is_login_password(self, state: FlowState) -> bool:
        return state.page_type == "login_password"

    def _state_is_email_otp(self, state: FlowState) -> bool:
        target = f"{state.continue_url} {state.current_url}".lower()
        return state.page_type == "email_otp_verification" or "email-verification" in target or "email-otp" in target

    def _state_requires_navigation(self, state: FlowState) -> bool:
        method = (state.method or "GET").upper()
        if method != "GET":
            return False
        if (
            state.source == "api"
            and state.current_url
            and state.page_type not in {"login_password", "email_otp_verification"}
        ):
            return True
        if state.page_type == "external_url" and state.continue_url:
            return True
        if state.continue_url and state.continue_url != state.current_url:
            return True
        return False

    def _decode_oauth_session_cookie(self) -> Optional[Dict[str, Any]]:
        jar = getattr(self.session.cookies, "jar", None)
        cookie_items = list(jar) if jar is not None else []
        fallback_data: Optional[Dict[str, Any]] = None

        for cookie in cookie_items:
            name = getattr(cookie, "name", "") or ""
            if "oai-client-auth-session" not in name:
                continue
            raw_value = (getattr(cookie, "value", "") or "").strip()
            if not raw_value:
                continue
            candidates = [raw_value]
            try:
                decoded = unquote(raw_value)
                if decoded != raw_value:
                    candidates.append(decoded)
            except Exception:
                pass
            try:
                decoded = raw_value.encode("utf-8").decode("unicode_escape")
                if decoded != raw_value:
                    candidates.append(decoded)
            except Exception:
                pass
            for value in candidates:
                try:
                    if (value.startswith('"') and value.endswith('"')) or (value.startswith("'") and value.endswith("'")):
                        value = value[1:-1]
                    part = value.split(".")[0] if "." in value else value
                    pad = 4 - len(part) % 4
                    if pad != 4:
                        part += "=" * pad
                    raw = base64.urlsafe_b64decode(part)
                    data = json.loads(raw.decode("utf-8"))
                    if isinstance(data, dict):
                        if data.get("workspaces"):
                            return data
                        if fallback_data is None:
                            fallback_data = data
                except Exception:
                    continue
        return fallback_data

    def _fetch_consent_page_html(self, consent_url: str) -> str:
        try:
            headers = self._headers(
                consent_url,
                accept="text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                referer=f"{self.config.oauth_issuer}/email-verification",
                navigation=True,
            )
            kwargs = {"headers": headers, "allow_redirects": True, "timeout": 30}
            if self.config.impersonate:
                kwargs["impersonate"] = self.config.impersonate
            self._browser_pause()
            response = self.session.get(consent_url, **kwargs)
            self._last_follow_url = str(response.url) or consent_url
            if response.status_code == 200 and "text/html" in (response.headers.get("content-type", "").lower()):
                return response.text
        except Exception:
            pass
        return ""

    def _extract_session_data_from_consent_html(self, html: str) -> Optional[Dict[str, Any]]:
        if not html or "workspaces" not in html:
            return None

        def first_match(patterns, text):
            for pattern in patterns:
                matched = re.search(pattern, text, re.S)
                if matched:
                    return matched.group(1)
            return ""

        def build_from_text(text: str) -> Optional[Dict[str, Any]]:
            if not text or "workspaces" not in text:
                return None
            normalized = text.replace('\\"', '"')

            session_id = first_match(
                [r'"session_id","([^"]+)"', r'"session_id":"([^"]+)"'],
                normalized,
            )
            client_id = first_match(
                [r'"openai_client_id","([^"]+)"', r'"openai_client_id":"([^"]+)"'],
                normalized,
            )

            start = normalized.find('"workspaces"')
            if start < 0:
                start = normalized.find("workspaces")
            if start < 0:
                return None

            end = normalized.find('"openai_client_id"', start)
            if end < 0:
                end = normalized.find("openai_client_id", start)
            if end < 0:
                end = min(len(normalized), start + 4000)
            else:
                end = min(len(normalized), end + 600)

            workspace_chunk = normalized[start:end]
            ids = re.findall(r'"id"(?:,|:)"([0-9a-fA-F-]{36})"', workspace_chunk)
            if not ids:
                return None

            kinds = re.findall(r'"kind"(?:,|:)"([^"]+)"', workspace_chunk)
            workspaces = []
            seen = set()
            for idx, workspace_id in enumerate(ids):
                if workspace_id in seen:
                    continue
                seen.add(workspace_id)
                item = {"id": workspace_id}
                if idx < len(kinds):
                    item["kind"] = kinds[idx]
                workspaces.append(item)

            if not workspaces:
                return None

            return {
                "session_id": session_id,
                "openai_client_id": client_id,
                "workspaces": workspaces,
            }

        candidates = [html]
        for quoted in re.findall(r'streamController\.enqueue\(("(?:\\.|[^"\\])*")\)', html, re.S):
            try:
                decoded = json.loads(quoted)
            except Exception:
                continue
            if decoded:
                candidates.append(decoded)

        if '\\"' in html:
            candidates.append(html.replace('\\"', '"'))

        for candidate in candidates:
            parsed = build_from_text(candidate)
            if parsed and parsed.get("workspaces"):
                return parsed
        return None

    def _load_workspace_session_data(self, consent_url: str) -> Optional[Dict[str, Any]]:
        session_data = self._decode_oauth_session_cookie()
        if session_data and session_data.get("workspaces"):
            return session_data

        html = self._fetch_consent_page_html(consent_url)
        if not html:
            return session_data

        parsed = self._extract_session_data_from_consent_html(html)
        if parsed and parsed.get("workspaces"):
            merged = dict(session_data or {})
            merged.update(parsed)
            self._log(f"从 consent HTML 提取到 {len(merged.get('workspaces', []))} 个 workspace")
            return merged

        return session_data

    def _state_supports_workspace_resolution(self, state: FlowState) -> bool:
        target = f"{state.continue_url} {state.current_url}".lower()
        if state.page_type in {"consent", "workspace_selection", "organization_selection", "add_phone"}:
            return True
        if any(marker in target for marker in ("sign-in-with-chatgpt", "consent", "workspace", "organization", "add-phone")):
            return True
        session_data = self._decode_oauth_session_cookie() or {}
        return bool(session_data.get("workspaces"))

    def _follow_flow_state(self, state: FlowState, referer: Optional[str] = None, max_hops: int = 16) -> Tuple[Optional[str], FlowState]:
        current_url = normalize_flow_url(state.continue_url or state.current_url, auth_base=self.config.oauth_issuer)
        last_url = current_url or ""
        referer_url = referer

        if not current_url:
            return None, state

        code = self._extract_code_from_url(current_url)
        if code:
            return code, self._state_from_url(current_url)

        for hop in range(max_hops):
            try:
                headers = self._headers(
                    current_url,
                    accept="text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                    referer=referer_url,
                    navigation=True,
                    extra_headers={"oai-device-id": self.device_id or None},
                )
                kwargs = {"headers": headers, "allow_redirects": False, "timeout": 30}
                if self.config.impersonate:
                    kwargs["impersonate"] = self.config.impersonate

                self._browser_pause()
                response = self.session.get(current_url, **kwargs)
                last_url = str(response.url)
                self._last_follow_url = last_url or current_url
                self._log(f"follow[{hop + 1}] {response.status_code} {last_url[:120]}")
            except Exception as exc:
                maybe_localhost = re.search(r'(https?://localhost[^\s\'\"]+)', str(exc))
                if maybe_localhost:
                    location = maybe_localhost.group(1)
                    code = self._extract_code_from_url(location)
                    if code:
                        self._log("从 localhost 异常提取到 authorization code")
                        return code, self._state_from_url(location)
                self._log(f"follow[{hop + 1}] 异常: {str(exc)[:160]}")
                return None, self._state_from_url(last_url or current_url)

            code = self._extract_code_from_url(last_url)
            if code:
                return code, self._state_from_url(last_url)

            if response.status_code in (301, 302, 303, 307, 308):
                location = normalize_flow_url(response.headers.get("Location", ""), auth_base=self.config.oauth_issuer)
                if not location:
                    return None, self._state_from_url(last_url or current_url)
                code = self._extract_code_from_url(location)
                if code:
                    return code, self._state_from_url(location)
                referer_url = last_url or referer_url
                current_url = location
                continue

            content_type = (response.headers.get("content-type", "") or "").lower()
            if "application/json" in content_type:
                try:
                    next_state = self._state_from_payload(response.json(), current_url=last_url or current_url)
                except Exception:
                    next_state = self._state_from_url(last_url or current_url)
            else:
                next_state = self._state_from_url(last_url or current_url)

            return None, next_state

        return None, self._state_from_url(last_url or current_url)

    def _build_sentinel_token(self, flow: str) -> Optional[str]:
        if not self.sentinel_builder:
            return None
        try:
            return self.sentinel_builder(
                self.session,
                self.device_id,
                flow=flow,
                user_agent=self.config.user_agent,
                sec_ch_ua=self.config.sec_ch_ua,
                impersonate=self.config.impersonate,
            )
        except Exception:
            return None

    def _bootstrap_oauth_session(self, authorize_url: str, authorize_params: Dict[str, Any]) -> str:
        _seed_oai_device_cookie(self.session, self.device_id)

        authorize_final_url = ""
        try:
            headers = self._headers(
                authorize_url,
                accept="text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                referer="https://chatgpt.com/",
                navigation=True,
            )
            kwargs = {
                "params": authorize_params,
                "headers": headers,
                "allow_redirects": True,
                "timeout": 30,
            }
            if self.config.impersonate:
                kwargs["impersonate"] = self.config.impersonate
            self._browser_pause()
            response = self.session.get(authorize_url, **kwargs)
            authorize_final_url = str(response.url)
            self._log(f"/oauth/authorize -> {response.status_code}, redirects={len(getattr(response, 'history', []) or [])}")
        except Exception as exc:
            self._log(f"/oauth/authorize 异常: {exc}")

        has_login_session = any((getattr(cookie, "name", "") or "") == "login_session" for cookie in self.session.cookies)
        if has_login_session:
            return authorize_final_url

        self._log("未获取到 login_session，尝试 /api/oauth/oauth2/auth...")
        try:
            oauth2_url = f"{self.config.oauth_issuer}/api/oauth/oauth2/auth"
            headers = self._headers(
                oauth2_url,
                accept="text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                referer="https://chatgpt.com/",
                navigation=True,
            )
            kwargs = {
                "params": authorize_params,
                "headers": headers,
                "allow_redirects": True,
                "timeout": 30,
            }
            if self.config.impersonate:
                kwargs["impersonate"] = self.config.impersonate
            self._browser_pause()
            response = self.session.get(oauth2_url, **kwargs)
            authorize_final_url = str(response.url)
            self._log(f"/api/oauth/oauth2/auth -> {response.status_code}, redirects={len(getattr(response, 'history', []) or [])}")
        except Exception as exc:
            self._log(f"/api/oauth/oauth2/auth 异常: {exc}")

        return authorize_final_url

    def _submit_authorize_continue(self, authorize_url: str, authorize_params: Dict[str, Any], continue_referer: str) -> Optional[FlowState]:
        self._log("2/7 POST /api/accounts/authorize/continue")
        sentinel_token = self._build_sentinel_token("authorize_continue")
        if not sentinel_token:
            self._log("无法获取 sentinel token (authorize_continue)")
            return None

        request_url = f"{self.config.oauth_issuer}/api/accounts/authorize/continue"
        headers = self._headers(
            request_url,
            accept="application/json",
            referer=continue_referer,
            origin=self.config.oauth_issuer,
            content_type="application/json",
            fetch_site="same-origin",
            extra_headers={
                "oai-device-id": self.device_id,
                "openai-sentinel-token": sentinel_token,
            },
        )
        headers.update(_generate_datadog_trace())

        def do_request(local_headers: Dict[str, str]):
            kwargs = {
                "json": {"username": {"kind": "email", "value": self.email}},
                "headers": local_headers,
                "timeout": 30,
                "allow_redirects": False,
            }
            if self.config.impersonate:
                kwargs["impersonate"] = self.config.impersonate
            self._browser_pause()
            return self.session.post(request_url, **kwargs)

        try:
            response = do_request(headers)
            self._log(f"authorize/continue -> {response.status_code}")
        except Exception as exc:
            self._log(f"authorize/continue 异常: {exc}")
            return None

        if response.status_code == 400 and "invalid_auth_step" in (response.text or ""):
            self._log("invalid_auth_step，重新 bootstrap...")
            authorize_final_url = self._bootstrap_oauth_session(authorize_url, authorize_params)
            continue_referer = authorize_final_url if authorize_final_url.startswith(self.config.oauth_issuer) else f"{self.config.oauth_issuer}/log-in"
            headers["Referer"] = continue_referer
            headers["Sec-Fetch-Site"] = "same-origin"
            headers.update(_generate_datadog_trace())
            try:
                response = do_request(headers)
                self._log(f"authorize/continue(重试) -> {response.status_code}")
            except Exception as exc:
                self._log(f"authorize/continue(重试) 异常: {exc}")
                return None

        if response.status_code != 200:
            self._log(f"提交邮箱失败: {response.text[:180]}")
            return None

        try:
            data = response.json()
        except Exception:
            self._log("authorize/continue JSON 解析失败")
            return None

        state = self._state_from_payload(data, current_url=str(response.url) or request_url)
        self._log(describe_flow_state(state))
        return state

    def _submit_password_verify(self, state: FlowState) -> Optional[FlowState]:
        self._log("3/7 POST /api/accounts/password/verify")
        sentinel_token = self._build_sentinel_token("password_verify")
        if not sentinel_token:
            self._log("无法获取 sentinel token (password_verify)")
            return None

        request_url = f"{self.config.oauth_issuer}/api/accounts/password/verify"
        headers = self._headers(
            request_url,
            accept="application/json",
            referer=state.current_url or state.continue_url or f"{self.config.oauth_issuer}/log-in/password",
            origin=self.config.oauth_issuer,
            content_type="application/json",
            fetch_site="same-origin",
            extra_headers={
                "oai-device-id": self.device_id,
                "openai-sentinel-token": sentinel_token,
            },
        )
        headers.update(_generate_datadog_trace())

        try:
            kwargs = {
                "json": {"password": self.password},
                "headers": headers,
                "timeout": 30,
                "allow_redirects": False,
            }
            if self.config.impersonate:
                kwargs["impersonate"] = self.config.impersonate
            self._browser_pause()
            response = self.session.post(request_url, **kwargs)
            self._log(f"password/verify -> {response.status_code}")
        except Exception as exc:
            self._log(f"password/verify 异常: {exc}")
            return None

        if response.status_code != 200:
            self._log(f"密码验证失败: {response.text[:180]}")
            return None

        try:
            data = response.json()
        except Exception:
            self._log("password/verify JSON 解析失败")
            return None

        next_state = self._state_from_payload(data, current_url=str(response.url) or request_url)
        self._log(f"verify {describe_flow_state(next_state)}")
        return next_state

    def _prime_email_verification_page(self, state: FlowState) -> None:
        target_url = state.current_url or state.continue_url or f"{self.config.oauth_issuer}/email-verification"
        try:
            kwargs = {
                "headers": self._headers(
                    target_url,
                    accept="text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                    referer=f"{self.config.oauth_issuer}/log-in",
                    navigation=True,
                    extra_headers={"oai-device-id": self.device_id},
                ),
                "allow_redirects": True,
                "timeout": 30,
            }
            if self.config.impersonate:
                kwargs["impersonate"] = self.config.impersonate
            self._browser_pause()
            response = self.session.get(target_url, **kwargs)
            self._log(f"email-verification 页面预热 -> {response.status_code}")
        except Exception as exc:
            self._log(f"email-verification 页面预热异常: {exc}")

    def _send_email_otp(self, state: FlowState, reason: str = "") -> bool:
        request_url = f"{self.config.oauth_issuer}/api/accounts/email-otp/send"
        referer = state.current_url or state.continue_url or f"{self.config.oauth_issuer}/email-verification"
        headers = self._headers(
            request_url,
            accept="application/json, text/plain, */*",
            referer=referer,
            fetch_site="same-origin",
            extra_headers={"oai-device-id": self.device_id},
        )
        headers.update(_generate_datadog_trace())

        try:
            kwargs = {
                "headers": headers,
                "allow_redirects": True,
                "timeout": 30,
            }
            if self.config.impersonate:
                kwargs["impersonate"] = self.config.impersonate
            self._browser_pause()
            response = self.session.get(request_url, **kwargs)
            suffix = f" ({reason})" if reason else ""
            self._log(f"email-otp/send -> {response.status_code}{suffix}")
            return response.status_code == 200
        except Exception as exc:
            self._log(f"email-otp/send 异常: {exc}")
            return False

    def _handle_otp_verification(self, state: FlowState, trigger_send: bool = False) -> Optional[FlowState]:
        self._log("4/7 检测到邮箱 OTP 验证")
        request_url = f"{self.config.oauth_issuer}/api/accounts/email-otp/validate"
        headers = self._headers(
            request_url,
            accept="application/json",
            referer=state.current_url or state.continue_url or f"{self.config.oauth_issuer}/email-verification",
            origin=self.config.oauth_issuer,
            content_type="application/json",
            fetch_site="same-origin",
            extra_headers={"oai-device-id": self.device_id},
        )
        headers.update(_generate_datadog_trace())

        self._prime_email_verification_page(state)

        tried_codes = set()
        deadline = time.time() + 90
        otp_sent_at = time.time()
        email_id = self.email_info.get("service_id")
        resend_marks = [0, 20, 45]
        resend_index = 0

        if trigger_send:
            if self._send_email_otp(state, reason="initial"):
                otp_sent_at = time.time()
                resend_index = 1

        while time.time() < deadline:
            elapsed = int(time.time() - (deadline - 90))
            while resend_index < len(resend_marks) and elapsed >= resend_marks[resend_index]:
                if resend_marks[resend_index] > 0:
                    if self._send_email_otp(state, reason=f"retry+{resend_marks[resend_index]}s"):
                        otp_sent_at = time.time()
                resend_index += 1

            try:
                code = self.email_service.get_verification_code(
                    email=self.email,
                    email_id=email_id,
                    timeout=5,
                    otp_sent_at=otp_sent_at,
                )
            except Exception as exc:
                self._log(f"等待 OTP 异常: {exc}")
                code = None

            if not code:
                self._log("暂未收到新的 OTP，继续等待...")
                continue

            if code in tried_codes:
                self._log(f"跳过已尝试验证码: {code}")
                continue

            tried_codes.add(code)
            self._log(f"尝试 OTP: {code}")
            try:
                kwargs = {
                    "json": {"code": code},
                    "headers": headers,
                    "timeout": 30,
                    "allow_redirects": False,
                }
                if self.config.impersonate:
                    kwargs["impersonate"] = self.config.impersonate
                self._browser_pause()
                response = self.session.post(request_url, **kwargs)
                self._log(f"/email-otp/validate -> {response.status_code}")
            except Exception as exc:
                self._log(f"email-otp/validate 异常: {exc}")
                continue

            if response.status_code != 200:
                self._log(f"OTP 无效: {response.text[:160]}")
                continue

            try:
                data = response.json()
            except Exception:
                self._log("email-otp/validate 响应不是 JSON")
                continue

            next_state = self._state_from_payload(
                data,
                current_url=str(response.url) or (state.current_url or state.continue_url or request_url),
            )
            self._log(f"OTP 验证通过 {describe_flow_state(next_state)}")
            return next_state

        self._log(f"OAuth 阶段 OTP 验证失败，已尝试 {len(tried_codes)} 个验证码")
        return None

    def _oauth_follow_for_code(self, start_url: str, referer: str, max_hops: int = 16) -> Tuple[Optional[str], str]:
        code, next_state = self._follow_flow_state(
            self._state_from_url(start_url),
            referer=referer,
            max_hops=max_hops,
        )
        return code, (next_state.current_url or next_state.continue_url or start_url)

    def _oauth_submit_workspace_and_org(self, consent_url: str) -> Tuple[Optional[str], Optional[FlowState]]:
        session_data = None
        for attempt in range(3):
            session_data = self._load_workspace_session_data(consent_url)
            if session_data:
                break
            if attempt < 2:
                self._log(f"无法获取 consent session 数据 (尝试 {attempt + 1}/3)")
                time.sleep(0.3)
            else:
                self._log("无法获取 consent session 数据")
                return None, None

        workspaces = session_data.get("workspaces", [])
        if not workspaces:
            if self.preferred_workspace_id:
                self._log(f"使用复用会话 Workspace ID: {self.preferred_workspace_id}")
                workspaces = [{"id": self.preferred_workspace_id, "kind": "personal"}]
            else:
                self._log(f"session cookie 无 workspaces, keys={list(session_data.keys())}")
                return None, None

        workspace_id = (workspaces[0] or {}).get("id")
        if not workspace_id:
            self._log("workspace_id 为空")
            return None, None

        self._log(f"选择 workspace: {workspace_id}")
        request_url = f"{self.config.oauth_issuer}/api/accounts/workspace/select"
        headers = self._headers(
            request_url,
            accept="application/json",
            referer=consent_url,
            origin=self.config.oauth_issuer,
            content_type="application/json",
            fetch_site="same-origin",
            extra_headers={"oai-device-id": self.device_id},
        )
        headers.update(_generate_datadog_trace())

        try:
            kwargs = {
                "json": {"workspace_id": workspace_id},
                "headers": headers,
                "allow_redirects": False,
                "timeout": 30,
            }
            if self.config.impersonate:
                kwargs["impersonate"] = self.config.impersonate
            self._browser_pause()
            response = self.session.post(request_url, **kwargs)
            self._log(f"workspace/select -> {response.status_code}")
        except Exception as exc:
            self._log(f"workspace/select 异常: {exc}")
            return None, None

        if response.status_code in (301, 302, 303, 307, 308):
            location = normalize_flow_url(response.headers.get("Location", ""), auth_base=self.config.oauth_issuer)
            code = self._extract_code_from_url(location)
            if code:
                self._log("从 workspace/select 重定向获取到 code")
                return code, self._state_from_url(location)
            if location:
                return None, self._state_from_url(location)

        if response.status_code != 200:
            return None, None

        try:
            data = response.json()
        except Exception:
            return None, None

        workspace_state = self._state_from_payload(data, current_url=str(response.url))
        continue_url = workspace_state.continue_url
        orgs = data.get("data", {}).get("orgs", [])

        if orgs:
            org_id = (orgs[0] or {}).get("id")
            projects = (orgs[0] or {}).get("projects", [])
            project_id = (projects[0] or {}).get("id") if projects else None

            if org_id:
                self._log(f"选择 organization: {org_id}")
                org_body = {"org_id": org_id}
                if project_id:
                    org_body["project_id"] = project_id

                org_referer = continue_url if continue_url and continue_url.startswith("http") else consent_url
                request_url = f"{self.config.oauth_issuer}/api/accounts/organization/select"
                headers = self._headers(
                    request_url,
                    accept="application/json",
                    referer=org_referer,
                    origin=self.config.oauth_issuer,
                    content_type="application/json",
                    fetch_site="same-origin",
                    extra_headers={"oai-device-id": self.device_id},
                )
                headers.update(_generate_datadog_trace())

                try:
                    kwargs = {
                        "json": org_body,
                        "headers": headers,
                        "allow_redirects": False,
                        "timeout": 30,
                    }
                    if self.config.impersonate:
                        kwargs["impersonate"] = self.config.impersonate
                    self._browser_pause()
                    response = self.session.post(request_url, **kwargs)
                    self._log(f"organization/select -> {response.status_code}")
                except Exception as exc:
                    self._log(f"organization/select 异常: {exc}")
                else:
                    if response.status_code in (301, 302, 303, 307, 308):
                        location = normalize_flow_url(response.headers.get("Location", ""), auth_base=self.config.oauth_issuer)
                        code = self._extract_code_from_url(location)
                        if code:
                            self._log("从 organization/select 重定向获取到 code")
                            return code, self._state_from_url(location)
                        if location:
                            return None, self._state_from_url(location)
                    if response.status_code == 200:
                        try:
                            org_state = self._state_from_payload(response.json(), current_url=str(response.url))
                            self._log(f"organization/select -> {describe_flow_state(org_state)}")
                            code = self._extract_code_from_state(org_state)
                            if code:
                                return code, org_state
                            return None, org_state
                        except Exception as exc:
                            self._log(f"解析 organization/select 响应异常: {exc}")

        if continue_url:
            code, _ = self._oauth_follow_for_code(continue_url, consent_url)
            if code:
                return code, self._state_from_url(continue_url)
        return None, workspace_state

    def _exchange_code_for_tokens(self, code: str, code_verifier: str) -> Optional[Dict[str, Any]]:
        request_url = f"{self.config.oauth_issuer}/oauth/token"
        headers = self._headers(
            request_url,
            accept="application/json",
            referer=f"{self.config.oauth_issuer}/sign-in-with-chatgpt/codex/consent",
            origin=self.config.oauth_issuer,
            content_type="application/x-www-form-urlencoded",
            fetch_site="same-origin",
        )

        payload = {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": self.config.oauth_redirect_uri,
            "client_id": self.config.oauth_client_id,
            "code_verifier": code_verifier,
        }

        try:
            kwargs = {"data": payload, "headers": headers, "timeout": 60}
            if self.config.impersonate:
                kwargs["impersonate"] = self.config.impersonate
            self._browser_pause()
            response = self.session.post(request_url, **kwargs)
            if response.status_code == 200:
                return response.json()
            self._log(f"换取 tokens 失败: {response.status_code} - {response.text[:200]}")
        except Exception as exc:
            self._log(f"换取 tokens 异常: {exc}")
        return None

    def run(self) -> Optional[Dict[str, Any]]:
        self._log("开始执行 OAuth 登录流程...")

        existing_session_data = self._decode_oauth_session_cookie() or {}
        preserve_workspace_cookie = bool(existing_session_data.get("workspaces"))
        if preserve_workspace_cookie:
            self._log(
                f"检测到现有 auth session 含 {len(existing_session_data.get('workspaces', []))} 个 workspace，保留 oai-client-auth-session"
            )

        jar = getattr(self.session.cookies, "jar", None)
        removed = 0
        if jar is not None:
            for cookie in list(jar):
                name = getattr(cookie, "name", "") or ""
                if preserve_workspace_cookie and "oai-client-auth-session" in name:
                    continue
                if name in (
                    "login_session",
                    "oai-client-auth-session",
                    "__Host-next-auth.csrf-token",
                    "__Secure-next-auth.callback-url",
                    "__Secure-next-auth.session-token",
                ):
                    try:
                        jar.clear(getattr(cookie, "domain", ""), getattr(cookie, "path", "/"), cookie.name)
                        removed += 1
                    except Exception:
                        continue
        self._log(f"清除了 {removed} 个旧 auth cookies")

        if not self.device_id:
            self.device_id = str(self.session.cookies.get("oai-did") or uuid.uuid4())
        _seed_oai_device_cookie(self.session, self.device_id)

        code_verifier, code_challenge = _generate_pkce()
        oauth_state = secrets.token_urlsafe(32)
        authorize_params = {
            "response_type": "code",
            "client_id": self.config.oauth_client_id,
            "redirect_uri": self.config.oauth_redirect_uri,
            "scope": self.config.oauth_scope,
            "state": oauth_state,
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
            "prompt": "login",
            "id_token_add_organizations": "true",
            "codex_cli_simplified_flow": "true",
        }
        authorize_url = f"{self.config.oauth_issuer}/oauth/authorize"

        self._log("1/7 GET /oauth/authorize")
        authorize_final_url = self._bootstrap_oauth_session(authorize_url, authorize_params)
        if not authorize_final_url:
            return None

        continue_referer = authorize_final_url if authorize_final_url.startswith(self.config.oauth_issuer) else f"{self.config.oauth_issuer}/log-in"
        state = self._submit_authorize_continue(authorize_url, authorize_params, continue_referer)
        if not state:
            return None

        seen_states = {}
        referer = continue_referer
        consent_fallback_used = False
        password_verified = False

        for _ in range(20):
            signature = self._state_signature(state)
            seen_states[signature] = seen_states.get(signature, 0) + 1
            if seen_states[signature] > 2:
                self._log(f"OAuth 状态卡住: {describe_flow_state(state)}")
                return None

            code = self._extract_code_from_state(state)
            if code:
                self._log(f"获取到 authorization code: {code[:20]}...")
                self._log("7/7 POST /oauth/token")
                tokens = self._exchange_code_for_tokens(code, code_verifier)
                if tokens:
                    self._log("OAuth 登录成功")
                return tokens

            if self._state_is_login_password(state):
                next_state = self._submit_password_verify(state)
                if not next_state:
                    return None
                password_verified = True
                referer = state.current_url or referer
                state = next_state
                continue

            if self._state_is_email_otp(state):
                next_state = self._handle_otp_verification(state, trigger_send=not password_verified)
                if not next_state:
                    return None
                referer = state.current_url or referer
                state = next_state
                continue

            if self._state_requires_navigation(state):
                code, next_state = self._follow_flow_state(state, referer=referer)
                if code:
                    self._log(f"获取到 authorization code: {code[:20]}...")
                    self._log("7/7 POST /oauth/token")
                    tokens = self._exchange_code_for_tokens(code, code_verifier)
                    if tokens:
                        self._log("OAuth 登录成功")
                    return tokens
                referer = state.current_url or referer
                state = next_state
                self._log(f"follow state -> {describe_flow_state(state)}")
                continue

            if self._state_supports_workspace_resolution(state):
                self._log("6/7 执行 workspace/org 选择")
                code, next_state = self._oauth_submit_workspace_and_org(
                    state.continue_url or state.current_url or f"{self.config.oauth_issuer}/sign-in-with-chatgpt/codex/consent"
                )
                if code:
                    self._log(f"获取到 authorization code: {code[:20]}...")
                    self._log("7/7 POST /oauth/token")
                    tokens = self._exchange_code_for_tokens(code, code_verifier)
                    if tokens:
                        self._log("OAuth 登录成功")
                    return tokens
                if next_state:
                    referer = state.current_url or referer
                    state = next_state
                    self._log(f"workspace state -> {describe_flow_state(state)}")
                    continue
                if not consent_fallback_used:
                    consent_fallback_used = True
                    self._log("6/7 回退 consent 路径重试")
                    state = self._state_from_url(f"{self.config.oauth_issuer}/sign-in-with-chatgpt/codex/consent")
                    continue

            self._log(f"未支持的 OAuth 状态: {describe_flow_state(state)}")
            return None

        self._log("OAuth 状态机超出最大步数")
        return None
