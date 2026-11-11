"""공개 HTTP(S) 링크 본문을 제한된 크기로 읽는다. 로그인·내부망 링크는 지원하지 않는다."""

import asyncio
from html.parser import HTMLParser
import ipaddress
import socket
from urllib.parse import urljoin, urlsplit

import httpx

from core.errors import AnalysisError


class PageText(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.hidden = 0
        self.parts = []

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style", "noscript"}:
            self.hidden += 1

    def handle_endtag(self, tag):
        if tag in {"script", "style", "noscript"} and self.hidden:
            self.hidden -= 1

    def handle_data(self, data):
        if not self.hidden and data.strip():
            self.parts.append(data.strip())


def validate_url(url):
    try:
        parsed = urlsplit(url)
        if (parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password
                or parsed.port not in {None, 80, 443} or len(url) > 4096):
            raise ValueError()
        return parsed
    except ValueError:
        raise AnalysisError(400, "invalid_url", "공개 HTTP/HTTPS 링크를 입력해 주세요.") from None


async def public_target(url):
    parsed = validate_url(url)
    try:
        addresses = await asyncio.wait_for(asyncio.to_thread(socket.getaddrinfo, parsed.hostname,
                                                           parsed.port or (443 if parsed.scheme == "https" else 80)), timeout=5)
    except (OSError, TimeoutError):
        raise AnalysisError(422, "link_unavailable", "링크 주소에 연결할 수 없어요.") from None
    if not addresses or any(not ipaddress.ip_address(address[4][0]).is_global for address in addresses):
        raise AnalysisError(400, "private_url", "로컬·내부망 주소는 링크 분석을 지원하지 않아요.")


async def fetch_link(url):
    try:
        async with httpx.AsyncClient(timeout=15, follow_redirects=False, trust_env=False) as client:
            for _ in range(4):
                await public_target(url)
                async with client.stream("GET", url, headers={"User-Agent": "GrowthArchive", "Accept": "text/html,text/plain"}) as response:
                    if response.is_redirect:
                        location = response.headers.get("location")
                        if not location:
                            break
                        url = urljoin(url, location)
                        continue
                    if response.status_code != 200:
                        raise AnalysisError(422, "link_unavailable", "링크 본문을 읽지 못했어요. 공개 접근 가능한 페이지인지 확인해 주세요.")
                    content_type = response.headers.get("content-type", "").split(";")[0]
                    if content_type not in {"text/html", "text/plain", "application/xhtml+xml"}:
                        raise AnalysisError(415, "link_content_type", "텍스트·HTML 페이지 링크만 분석할 수 있어요. 파일은 직접 첨부해 주세요.")
                    body = bytearray()
                    async for chunk in response.aiter_bytes():
                        body.extend(chunk)
                        if len(body) > 2 * 1024 * 1024:
                            raise AnalysisError(400, "link_too_large", "링크 페이지가 너무 커서 분석하지 못했어요.")
                    text = body.decode(response.encoding or "utf-8", errors="replace")
                    if content_type != "text/plain":
                        parser = PageText()
                        parser.feed(text)
                        text = "\n".join(parser.parts)
                    text = text.strip()
                    if not text or len(text) > 100_000:
                        raise AnalysisError(422, "link_content_invalid", "링크 본문이 비어 있거나 분석 범위를 초과했어요.")
                    return text
    except httpx.HTTPError:
        raise AnalysisError(422, "link_unavailable", "링크 본문 요청에 실패했어요.") from None
    raise AnalysisError(422, "link_redirects", "링크의 이동 횟수가 너무 많아요.")
