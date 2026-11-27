"""Zero-key DuckDuckGo web search and SSRF-guarded HTML-to-Markdown fetcher."""

from __future__ import annotations

import ipaddress
import re
import socket
from typing import Any
from urllib.parse import urljoin, urlsplit

import httpx
import httpcore
import httpcore._backends.sync
import trafilatura
from bs4 import BeautifulSoup
from ddgs import DDGS

_BLOCKED_HOSTNAMES = frozenset({"localhost", "localhost.localdomain", "ip6-localhost", "ip6-loopback"})
_BLOCKED_SUFFIXES = (".local", ".internal", ".localhost", ".localdomain", ".lan", ".home", ".arpa")
_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
_DEFAULT_HEADERS = {
    "User-Agent": "JEVDBStockResearch/0.1 (+https://jevdb.org/)",
    "Accept": "text/html,application/xhtml+xml,text/plain,application/json;q=0.9,*/*;q=0.8",
}
_MAX_HTML_BYTES = 2 * 1024 * 1024  # 2 MiB safety cap


class SSRFError(ValueError):
    """Raised when a URL fails SSRF validation or points to a disallowed network address."""


def _is_blocked_ip(ip_obj: ipaddress.IPv4Address | ipaddress.IPv6Address, *, allow_loopback: bool) -> bool:
    if ip_obj.is_loopback:
        return not allow_loopback
    return bool(
        ip_obj.is_private
        or ip_obj.is_link_local
        or ip_obj.is_reserved
        or ip_obj.is_multicast
        or ip_obj.is_unspecified
        or not ip_obj.is_global
    )


def _validate_and_resolve_url(
    url: str,
    *,
    allow_loopback_for_test: bool = False,
) -> tuple[str, bool, str, int, list[tuple[int, int, int, tuple[Any, ...]]]]:
    """Validate HTTP(S) URL, hostname, and resolved IPs against SSRF rules.

    Returns (normalized_url, is_loopback_target, norm_host, port, pinned_targets).
    """
    raw = str(url or "").strip()
    if not raw:
        raise SSRFError("URL must be non-empty")

    parsed = urlsplit(raw)
    scheme = (parsed.scheme or "").lower()
    if scheme not in ("http", "https"):
        raise SSRFError(f"Unsupported URL scheme {scheme!r}; only http and https are allowed")

    if parsed.username or parsed.password:
        raise SSRFError("URLs with embedded credentials are not allowed")

    host = (parsed.hostname or "").strip().strip("[]").rstrip(".").lower()
    if not host:
        raise SSRFError("URL must contain a valid hostname")

    # Never allow r.jina.ai proxy wrapper or internal/local hostnames
    if host == "r.jina.ai":
        raise SSRFError("Disallowed proxy host r.jina.ai")

    try:
        raw_port = parsed.port
    except ValueError as exc:
        raise SSRFError(f"Invalid URL port: {exc}") from exc
    port = int(raw_port or (443 if scheme == "https" else 80))
    is_loopback_target = False
    pinned_targets: list[tuple[int, int, int, tuple[Any, ...]]] = []

    # Check literal IP address first
    ip_literal: ipaddress.IPv4Address | ipaddress.IPv6Address | None = None
    try:
        ip_literal = ipaddress.ip_address(host)
    except ValueError:
        ip_literal = None

    if ip_literal is not None:
        if _is_blocked_ip(ip_literal, allow_loopback=allow_loopback_for_test):
            raise SSRFError(f"Blocked IP address target: {host}")
        is_loopback_target = ip_literal.is_loopback
        try:
            addr_infos = socket.getaddrinfo(
                str(ip_literal),
                port,
                type=socket.SOCK_STREAM,
                flags=socket.AI_NUMERICHOST,
            )
        except socket.gaierror as exc:
            raise SSRFError(f"Invalid literal IP target {host}: {exc}") from exc
        pinned_targets = [(info[0], info[1], info[2], info[4]) for info in addr_infos if info[4]]
    else:
        if host in _BLOCKED_HOSTNAMES or host.endswith(_BLOCKED_SUFFIXES):
            if allow_loopback_for_test and host == "localhost":
                is_loopback_target = True
            else:
                raise SSRFError(f"Blocked local/internal hostname: {host}")

        try:
            addr_infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
        except socket.gaierror as exc:
            raise SSRFError(f"DNS resolution failed for {host}: {exc}") from exc

        if not addr_infos:
            raise SSRFError(f"DNS resolution returned no addresses for {host}")

        for info in addr_infos:
            sockaddr = info[4]
            if not sockaddr:
                continue
            resolved_ip_str = str(sockaddr[0]).split("%")[0]
            try:
                resolved_ip = ipaddress.ip_address(resolved_ip_str)
            except ValueError as exc:
                raise SSRFError(f"Invalid resolved IP {resolved_ip_str!r} for {host}") from exc
            if _is_blocked_ip(resolved_ip, allow_loopback=allow_loopback_for_test):
                raise SSRFError(f"Hostname {host} resolved to disallowed IP {resolved_ip}")
            if resolved_ip.is_loopback:
                is_loopback_target = True
            pinned_targets.append((info[0], info[1], info[2], sockaddr))

    if not pinned_targets:
        raise SSRFError(f"No valid TCP endpoints resolved for {host}:{port}")

    return raw, is_loopback_target, host, port, pinned_targets


def _validate_url(url: str, *, allow_loopback_for_test: bool = False) -> tuple[str, bool]:
    """Backward-compatible wrapper returning (normalized_url, is_loopback_target)."""
    raw, is_loopback, _host, _port, _targets = _validate_and_resolve_url(
        url, allow_loopback_for_test=allow_loopback_for_test
    )
    return raw, is_loopback


class _PinnedNetworkBackend(httpcore.SyncBackend):
    """httpcore network backend connecting strictly to pre-validated pinned IPs (eliminates DNS-rebinding TOCTOU)."""

    def __init__(
        self,
        pinned_targets: dict[tuple[str, int], list[tuple[int, int, int, tuple[Any, ...]]]],
    ) -> None:
        self._pinned_targets = pinned_targets

    def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Any = None,
    ) -> httpcore.NetworkStream:
        norm_host = str(host or "").strip().strip("[]").rstrip(".").lower()
        targets = self._pinned_targets.get((norm_host, int(port)))
        if not targets:
            raise SSRFError(f"Refused unpinned TCP connection to {host}:{port}")

        if socket_options is None:
            socket_options = []

        last_exc: OSError | None = None
        for family, socktype, proto, sockaddr in targets:
            sock: socket.socket | None = None
            try:
                sock = socket.socket(family, socktype, proto)
                if timeout is not None:
                    sock.settimeout(timeout)
                if local_address is not None:
                    sock.bind((local_address, 0))
                sock.connect(sockaddr)
                for option in socket_options:
                    sock.setsockopt(*option)
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                return httpcore._backends.sync.SyncStream(sock)
            except socket.timeout as exc:
                last_exc = exc
                if sock is not None:
                    try:
                        sock.close()
                    except OSError:
                        pass
            except OSError as exc:
                last_exc = exc
                if sock is not None:
                    try:
                        sock.close()
                    except OSError:
                        pass

        if isinstance(last_exc, socket.timeout):
            raise httpcore.ConnectTimeout(str(last_exc)) from last_exc
        raise httpcore.ConnectError(str(last_exc or f"Failed to connect to {host}:{port}")) from last_exc


def _render_inline_markdown(node: Any, *, source_url: str) -> str:
    parts: list[str] = []
    for child in getattr(node, "children", []):
        name = getattr(child, "name", None)
        if name is None:
            parts.append(str(child))
        elif name == "a":
            href = (child.get("href") or "").strip()
            link_text = " ".join(child.get_text(" ", strip=True).split())
            abs_href = urljoin(source_url, href) if href else ""
            if abs_href.startswith(("http://", "https://")) and link_text:
                parts.append(f"[{link_text}]({abs_href})")
            elif link_text:
                parts.append(link_text)
        else:
            parts.append(_render_inline_markdown(child, source_url=source_url))
    return re.sub(r"[ \t]+", " ", "".join(parts)).strip()


def _render_table_markdown(table_tag: Any, *, source_url: str) -> str:
    rows_md: list[str] = []
    trs = table_tag.find_all("tr")
    if not trs:
        return ""
    col_count = 0
    parsed_rows: list[tuple[bool, list[str]]] = []
    for tr in trs:
        cells = tr.find_all(["th", "td"], recursive=False) or tr.find_all(["th", "td"])
        if not cells:
            continue
        is_header = any(c.name == "th" for c in cells)
        texts = [_render_inline_markdown(c, source_url=source_url).replace("|", "\\|") for c in cells]
        col_count = max(col_count, len(texts))
        parsed_rows.append((is_header, texts))
    if not parsed_rows or col_count == 0:
        return ""
    for idx, (_is_hdr, texts) in enumerate(parsed_rows):
        padded = texts + [""] * (col_count - len(texts))
        rows_md.append("| " + " | ".join(padded) + " |")
        if idx == 0:
            rows_md.append("| " + " | ".join(["---"] * col_count) + " |")
    return "\n".join(rows_md)


def _html_to_markdown(html_text: str, *, source_url: str, max_chars: int) -> str:
    soup = BeautifulSoup(html_text, "html.parser")
    for tag in soup(["script", "style", "noscript", "iframe", "svg"]):
        tag.decompose()

    cleaned_html = str(soup)
    extracted = trafilatura.extract(
        cleaned_html,
        url=source_url,
        output_format="markdown",
        include_tables=True,
        include_links=True,
        favor_recall=True,
    )

    if not extracted or not extracted.strip():
        lines: list[str] = []
        for elem in soup.find_all(["h1", "h2", "h3", "h4", "p", "li", "table", "pre"]):
            if elem.name in ("p", "li") and elem.find_parent("table") is not None:
                continue
            if elem.name == "table":
                tbl_md = _render_table_markdown(elem, source_url=source_url)
                if tbl_md:
                    lines.append(tbl_md)
                continue
            txt = _render_inline_markdown(elem, source_url=source_url)
            if not txt:
                continue
            if elem.name in ("h1", "h2", "h3", "h4"):
                level = int(elem.name[1])
                lines.append(f"{'#' * level} {txt}")
            elif elem.name == "li":
                lines.append(f"- {txt}")
            else:
                lines.append(txt)
        extracted = "\n\n".join(lines) if lines else soup.get_text("\n", strip=True)
    else:
        # Preserve <a href> links and <table> markdown formatting when trafilatura baseline strips them
        extra_links: list[str] = []
        for a_tag in soup.find_all("a", href=True):
            href = (a_tag.get("href") or "").strip()
            abs_href = urljoin(source_url, href) if href else ""
            if not abs_href.startswith(("http://", "https://")):
                continue
            if abs_href in extracted:
                continue
            anchor_txt = " ".join(a_tag.get_text(" ", strip=True).split())
            if not anchor_txt:
                continue
            if len(anchor_txt) >= 2:
                pat = re.compile(
                    rf"(?<!\[)(?<![A-Za-z0-9_]){re.escape(anchor_txt)}(?![A-Za-z0-9_])(?!\]\()"
                )
                updated, n_sub = pat.subn(f"[{anchor_txt}]({abs_href})", extracted, count=1)
                if n_sub > 0:
                    extracted = updated
                    continue
            extra_links.append(f"- [{anchor_txt}]({abs_href})")

        if soup.find("table") is not None and "| " not in extracted:
            tbl_blocks = [
                _render_table_markdown(t, source_url=source_url) for t in soup.find_all("table")
            ]
            tbl_blocks = [b for b in tbl_blocks if b]
            if tbl_blocks:
                extracted = extracted.rstrip() + "\n\n" + "\n\n".join(tbl_blocks)

        if extra_links:
            extracted = extracted.rstrip() + "\n\n" + "\n".join(extra_links)

    markdown = re.sub(r"\n{3,}", "\n\n", (extracted or "").strip())
    if max_chars > 0 and len(markdown) > max_chars:
        markdown = markdown[:max_chars].rstrip() + "\n...[truncated]"
    return markdown


def web_search(
    query: str,
    max_results: int = 5,
    search_type: str = "text",
) -> list[dict[str, str]]:
    """Search the web via DuckDuckGo (ddgs) without API keys."""
    clean_query = str(query or "").strip()
    if not clean_query:
        raise ValueError("Search query must be non-empty")

    limit = max(1, min(int(max_results or 5), 25))
    mode = str(search_type or "text").strip().lower()
    if mode not in ("text", "news"):
        raise ValueError(f"Unsupported search_type {search_type!r}; expected 'text' or 'news'")

    def _normalize(raw_items: list[dict[str, Any]] | None) -> list[dict[str, str]]:
        out: list[dict[str, str]] = []
        for item in raw_items or []:
            if not isinstance(item, dict):
                continue
            raw_url = str(item.get("href") or item.get("url") or "").strip()
            if not raw_url.startswith(("http://", "https://")):
                continue
            if "r.jina.ai" in raw_url.lower():
                continue
            title = str(item.get("title") or "").strip()
            snippet = str(item.get("body") or item.get("snippet") or "").strip()
            if not title and not snippet:
                continue
            entry: dict[str, str] = {
                "title": title or raw_url,
                "url": raw_url,
                "snippet": snippet,
            }
            if item.get("date"):
                entry["published_at"] = str(item["date"])
            if item.get("source"):
                entry["source"] = str(item["source"])
            out.append(entry)
            if len(out) >= limit:
                break
        return out

    def _try_call(method_name: str, *args: Any, **kwargs: Any) -> list[dict[str, str]]:
        try:
            try:
                client = DDGS(timeout=8)
            except TypeError:
                client = DDGS()
            fn = getattr(client, method_name)
            try:
                raw = fn(*args, **kwargs)
            except TypeError:
                # Mock DDGS in unit tests only accepts (query, max_results=...)
                raw = fn(args[0], max_results=kwargs.get("max_results", limit * 2))
        except Exception:
            return []
        return _normalize(raw)

    if mode == "news":
        results = _try_call("news", clean_query, max_results=limit * 2)
        if not results:
            results = _try_call(
                "text", clean_query, max_results=limit * 2, backend="yahoo,duckduckgo,brave,mojeek"
            )
        if not results:
            results = _try_call("text", clean_query, max_results=limit * 2, backend="yahoo")
        return results

    # Use fast non-hanging engines (excluding startpage which times out) and fall back to yahoo / cn-zh / news.
    results = _try_call(
        "text", clean_query, max_results=limit * 2, backend="yahoo,duckduckgo,brave,mojeek"
    )
    if not results:
        results = _try_call("text", clean_query, max_results=limit * 2, backend="yahoo")
    if not results:
        results = _try_call("text", clean_query, max_results=limit * 2, region="cn-zh")
    if not results:
        results = _try_call("news", clean_query, max_results=limit * 2)
    return results


def web_fetch_url(
    url: str,
    *,
    allow_loopback_for_test: bool = False,
    timeout: float = 6.0,
    max_chars: int = 12000,
    max_redirects: int = 5,
) -> str:
    """Fetch a public HTTP(S) URL with strict SSRF & redirect checks and extract clean Markdown."""
    current_url, is_loopback, host, port, targets = _validate_and_resolve_url(
        url, allow_loopback_for_test=allow_loopback_for_test
    )

    # For loopback test servers, disable env HTTP(S)_PROXY so 127.0.0.1 requests connect directly.
    use_env_proxy = not is_loopback
    pinned_targets: dict[tuple[str, int], list[tuple[int, int, int, tuple[Any, ...]]]] = {
        (host, port): targets
    }

    resp: httpx.Response | None = None
    # Leave transport=None so HTTP_PROXY/HTTPS_PROXY mounts remain active, while pinning
    # direct connection pool sockets to pre-validated IPs (eliminates direct DNS-rebinding TOCTOU).
    with httpx.Client(
        follow_redirects=False,
        timeout=float(timeout),
        headers=_DEFAULT_HEADERS,
        trust_env=use_env_proxy,
    ) as client:
        pool = getattr(getattr(client, "_transport", None), "_pool", None)
        if isinstance(pool, httpcore.ConnectionPool):
            pool._network_backend = _PinnedNetworkBackend(pinned_targets)

        for _hop in range(max_redirects + 1):
            resp = client.get(current_url)
            if resp.status_code in _REDIRECT_STATUSES:
                location = (resp.headers.get("location") or "").strip()
                if not location:
                    raise ValueError(f"Redirect response ({resp.status_code}) missing Location header")
                next_url = urljoin(current_url, location)
                current_url, next_is_loopback, next_host, next_port, next_targets = _validate_and_resolve_url(
                    next_url, allow_loopback_for_test=allow_loopback_for_test
                )
                if next_is_loopback and use_env_proxy:
                    # Never bounce a loopback redirect through an external proxy
                    raise SSRFError(f"Blocked redirect to loopback target: {current_url}")
                pinned_targets[(next_host, next_port)] = next_targets
                continue
            break
        else:
            raise ValueError(f"Exceeded maximum redirects ({max_redirects}) fetching {url}")

    assert resp is not None
    resp.raise_for_status()

    content_bytes = resp.content[:_MAX_HTML_BYTES]
    encoding = resp.encoding or "utf-8"
    text = content_bytes.decode(encoding, errors="replace")
    content_type = (resp.headers.get("content-type") or "").lower()

    if "html" in content_type or "<html" in text[:1024].lower() or "<body" in text[:1024].lower():
        return _html_to_markdown(text, source_url=current_url, max_chars=max_chars)

    plain = text.strip()
    if max_chars > 0 and len(plain) > max_chars:
        plain = plain[:max_chars].rstrip() + "\n...[truncated]"
    return plain
