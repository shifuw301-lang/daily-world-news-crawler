#!/usr/bin/env python3
"""Crawl today's world news from public RSS feeds and write a Markdown digest.

Design constraints:
  * Only public, no-login RSS/Atom endpoints (no paywalled sources).
  * Polite crawling: robots.txt respected, one request per source per run,
    a minimum interval between requests to the same host, jitter, a global
    requests-per-minute ceiling, and 429/503 backoff with Retry-After support.
  * Standard library only (no third-party dependencies).
"""

from __future__ import annotations

import argparse
import gzip
import os
import random
import re
import socket
import sys
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlsplit

# Replace the contact address below with your own before publishing a public
# instance of this crawler. A reachable contact is what keeps a bot "polite".
BOT_NAME = "DailyWorldNewsCrawler"
BOT_VERSION = "1.0"
CONTACT = "operator@example.com"
USER_AGENT = f"{BOT_NAME}/{BOT_VERSION} (polite daily digest; contact: {CONTACT})"

# Public RSS/Atom endpoints only: no login, no paywall, no API key.
# Feeds whose robots.txt disallow non-search-engine clients are left out on
# purpose (e.g. news.un.org only allows /feed/subscribe/ for Feedfetcher-Google).
DEFAULT_SOURCES = [
    {
        "key": "bbc",
        "name": "BBC News",
        "region": "英国",
        "url": "https://feeds.bbci.co.uk/news/world/rss.xml",
    },
    {
        "key": "guardian",
        "name": "The Guardian",
        "region": "英国",
        "url": "https://www.theguardian.com/world/rss",
    },
    {
        "key": "aljazeera",
        "name": "Al Jazeera English",
        "region": "卡塔尔",
        "url": "https://www.aljazeera.com/xml/rss/all.xml",
    },
    {
        "key": "npr",
        "name": "NPR World",
        "region": "美国",
        "url": "https://feeds.npr.org/1004/rss.xml",
    },
    {
        "key": "dw",
        "name": "Deutsche Welle",
        "region": "德国",
        "url": "https://rss.dw.com/rdf/rss-en-all",
    },
    {
        "key": "france24",
        "name": "France 24",
        "region": "法国",
        "url": "https://www.france24.com/en/rss",
    },
    {
        "key": "skynews",
        "name": "Sky News",
        "region": "英国",
        "url": "https://feeds.skynews.com/feeds/rss/world.xml",
    },
    {
        "key": "cbc",
        "name": "CBC News",
        "region": "加拿大",
        "url": "https://www.cbc.ca/webfeed/rss/rss-world",
    },
    {
        "key": "anadolu",
        "name": "Anadolu Agency",
        "region": "土耳其",
        "url": "https://www.aa.com.tr/en/rss/default?cat=guncel",
    },
    {
        "key": "cna",
        "name": "CNA",
        "region": "新加坡",
        "url": "https://www.channelnewsasia.com/api/v1/rss-outbound-feed?_format=xml",
    },
    {
        "key": "abcau",
        "name": "ABC News (Australia)",
        "region": "澳大利亚",
        "url": "https://www.abc.net.au/news/feed/51120/rss.xml",
    },
    {
        "key": "euronews",
        "name": "Euronews",
        "region": "欧洲",
        "url": "https://www.euronews.com/rss?level=theme&name=news",
    },
]


@dataclass
class Source:
    key: str
    name: str
    region: str
    url: str


@dataclass
class Entry:
    title: str
    link: str
    summary: str
    source_key: str
    source_name: str
    region: str
    published: datetime | None


@dataclass
class SourceResult:
    source: Source
    entries: list[Entry] = field(default_factory=list)
    error: str = ""


# --------------------------------------------------------------------------- #
# Console helpers
# --------------------------------------------------------------------------- #

def enable_utf8_console() -> None:
    """Make Chinese output survive Windows consoles and piped capture."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass


# --------------------------------------------------------------------------- #
# Rate limiting
# --------------------------------------------------------------------------- #

class RateLimiter:
    """Per-host spacing plus a global sliding window of requests per minute."""

    def __init__(self, min_interval: float, max_per_minute: int) -> None:
        self.min_interval = max(0.5, min_interval)
        self.max_per_minute = max(1, max_per_minute)
        self._last_host: dict[str, float] = {}
        self._host_interval: dict[str, float] = {}
        self._window: deque[float] = deque()

    def set_host_interval(self, host: str, seconds: float) -> None:
        """Honour a robots.txt Crawl-delay that is longer than our own interval."""
        if seconds and seconds > self._host_interval.get(host, 0.0):
            self._host_interval[host] = float(seconds)

    def wait(self, host: str) -> None:
        while True:
            now = time.monotonic()
            while self._window and now - self._window[0] > 60.0:
                self._window.popleft()

            delay = 0.0
            last = self._last_host.get(host)
            if last is not None:
                base = max(self.min_interval, self._host_interval.get(host, 0.0))
                # Small jitter keeps a daily run from looking like a burst.
                interval = base * (1.0 + random.uniform(0.0, 0.2))
                delay = max(delay, last + interval - now)
            if len(self._window) >= self.max_per_minute:
                delay = max(delay, self._window[0] + 60.0 - now)
            if delay <= 0:
                break
            time.sleep(delay)

        self._last_host[host] = time.monotonic()
        self._window.append(self._last_host[host])


# --------------------------------------------------------------------------- #
# robots.txt
# --------------------------------------------------------------------------- #

class RobotsRules:
    """Minimal RFC 9309 matcher: supports * and $ wildcards, longest match wins.

    urllib.robotparser ignores wildcard rules and does not implement
    "longest matching rule wins", which turns explicitly allowed feed
    endpoints into false positives.
    """

    def __init__(self, agent: str) -> None:
        self._agent = agent.lower()
        self._rules: list[tuple[bool, int, re.Pattern[str]]] = []
        self.allow_all = False
        self.disallow_all = False
        self.crawl_delay: float | None = None

    @staticmethod
    def _compile(pattern: str) -> tuple[int, re.Pattern[str]]:
        anchored = pattern.endswith("$")
        body = pattern[:-1] if anchored else pattern
        regex = "".join(".*" if char == "*" else re.escape(char) for char in body)
        return len(pattern), re.compile("^" + regex + ("$" if anchored else ""))

    def parse(self, text: str) -> None:
        groups: list[dict] = []
        current: dict | None = None
        in_agent_block = False
        for raw_line in text.splitlines():
            line = raw_line.split("#", 1)[0].strip()
            if not line or ":" not in line:
                continue
            key, value = line.split(":", 1)
            key = key.strip().lower()
            value = value.strip()
            if key == "user-agent":
                if current is None or not in_agent_block:
                    current = {"agents": [], "rules": [], "delay": None}
                    groups.append(current)
                current["agents"].append(value.lower())
                in_agent_block = True
                continue
            in_agent_block = False
            if current is None:
                continue
            if key == "disallow" and value:
                current["rules"].append((False, value))
            elif key == "allow" and value:
                current["rules"].append((True, value))
            elif key == "crawl-delay":
                try:
                    current["delay"] = float(value)
                except ValueError:
                    pass

        matched = [group for group in groups if self._agent in group["agents"]]
        if not matched:
            matched = [group for group in groups if "*" in group["agents"]]
        rules = [rule for group in matched for rule in group["rules"]]
        for allow, pattern in rules:
            length, regex = self._compile(pattern)
            self._rules.append((allow, length, regex))
        for group in matched:
            if group["delay"]:
                self.crawl_delay = max(self.crawl_delay or 0.0, float(group["delay"]))

    def allows(self, url: str) -> bool:
        if self.disallow_all:
            return False
        if self.allow_all or not self._rules:
            return True
        parts = urlsplit(url)
        path = parts.path or "/"
        if parts.query:
            path = f"{path}?{parts.query}"
        best: tuple[int, bool] | None = None
        for allow, length, regex in self._rules:
            if not regex.match(path):
                continue
            if best is None or length > best[0] or (length == best[0] and allow and not best[1]):
                best = (length, allow)
        if best is None:
            return True
        return best[1]


class RobotsPolicy:
    """Caches robots.txt per origin. Unreachable rules are treated as allow."""

    def __init__(self, limiter: RateLimiter, timeout: float) -> None:
        self._limiter = limiter
        self._timeout = timeout
        self._cache: dict[str, RobotsRules | None] = {}
        self.warnings: list[str] = []

    def allowed(self, url: str) -> bool:
        parts = urlsplit(url)
        origin = f"{parts.scheme}://{parts.netloc}"
        if origin not in self._cache:
            self._cache[origin] = self._load(origin, parts.netloc)
        rules = self._cache[origin]
        if rules is None:
            return True
        if rules.crawl_delay:
            self._limiter.set_host_interval(parts.netloc, rules.crawl_delay)
        return rules.allows(url)

    def _load(self, origin: str, host: str) -> RobotsRules | None:
        rules = RobotsRules(BOT_NAME)
        robots_url = origin + "/robots.txt"
        self._limiter.wait(host)
        request = urllib.request.Request(robots_url, headers={"User-Agent": USER_AGENT})
        try:
            with urllib.request.urlopen(request, timeout=self._timeout) as response:
                body = response.read().decode("utf-8", "replace")
            rules.parse(body)
            return rules
        except urllib.error.HTTPError as exc:
            if exc.code in (401, 403):
                rules.disallow_all = True
                self.warnings.append(f"robots.txt 拒绝访问({exc.code})，跳过该站点：{origin}")
                return rules
            if 400 <= exc.code < 500:
                # RFC 9309: an unavailable rules file means "no restrictions".
                rules.allow_all = True
                return rules
            self.warnings.append(f"robots.txt 返回 {exc.code}，按无限制处理：{origin}")
            return None
        except Exception as exc:  # noqa: BLE001 - network errors are non-fatal here
            self.warnings.append(f"robots.txt 读取失败({exc})，按无限制处理：{origin}")
            return None


# --------------------------------------------------------------------------- #
# Network configuration
# --------------------------------------------------------------------------- #

def _windows_system_proxy() -> str:
    """Read the WinINET proxy (e.g. a local Clash/V2Ray port) on Windows."""
    if sys.platform != "win32":
        return ""
    try:
        import winreg  # noqa: PLC0415 - Windows-only, imported lazily
    except ImportError:
        return ""
    path = r"Software\Microsoft\Windows\CurrentVersion\Internet Settings"
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, path) as key:
            enabled = winreg.QueryValueEx(key, "ProxyEnable")[0]
            server = str(winreg.QueryValueEx(key, "ProxyServer")[0]).strip()
    except OSError:
        return ""
    if not enabled or not server:
        return ""
    if "=" in server:  # per-protocol form, e.g. "http=a:1;https=b:2"
        pairs = {}
        for item in server.split(";"):
            if "=" in item:
                scheme, value = item.split("=", 1)
                pairs[scheme.strip().lower()] = value.strip()
        server = pairs.get("https") or pairs.get("http") or ""
    if not server:
        return ""
    return server if "://" in server else "http://" + server


def _local_port_open(proxy: str) -> bool:
    """A stale loopback proxy setting makes every request hang; probe it first."""
    parts = urlsplit(proxy if "://" in proxy else "http://" + proxy)
    host = parts.hostname or ""
    try:
        port = parts.port
    except ValueError:
        return True
    if host not in ("127.0.0.1", "localhost", "::1"):
        return True  # never second-guess a remote/corporate proxy
    try:
        with socket.create_connection((host, port or 8080), timeout=0.8):
            return True
    except OSError:
        return False


def configure_network(explicit_proxy: str, disable_proxy: bool) -> tuple[str, str]:
    """Wire up a proxy when one is needed. Returns (proxy_in_use, warning)."""
    if disable_proxy:
        urllib.request.install_opener(urllib.request.build_opener(urllib.request.ProxyHandler({})))
        return "", ""
    proxy = explicit_proxy.strip()
    if not proxy:
        known = urllib.request.getproxies()
        if known.get("https") or known.get("http") or known.get("all"):
            return "", ""  # urllib's own environment handling is already in charge
        proxy = _windows_system_proxy()
    if not proxy:
        return "", ""
    if not _local_port_open(proxy):
        return "", f"代理 {proxy} 未在监听，已改为直连；如需代理请先启动代理软件或用 --proxy 指定"
    handler = urllib.request.ProxyHandler({"http": proxy, "https": proxy})
    urllib.request.install_opener(urllib.request.build_opener(handler))
    return proxy, ""


# --------------------------------------------------------------------------- #
# HTTP
# --------------------------------------------------------------------------- #

def _retry_after_seconds(header_value: str | None) -> float | None:
    if not header_value:
        return None
    header_value = header_value.strip()
    if header_value.isdigit():
        return float(header_value)
    try:
        parsed = parsedate_to_datetime(header_value)
        if parsed is not None:
            return max(0.0, (parsed - datetime.now(timezone.utc)).total_seconds())
    except (TypeError, ValueError):
        return None
    return None


def http_get(url: str, timeout: float, retries: int, limiter: RateLimiter) -> tuple[bytes | None, str]:
    """Fetch a URL politely. Returns (body, error_message)."""
    host = urlsplit(url).netloc
    last_error = "未知错误"
    for attempt in range(max(1, retries)):
        limiter.wait(host)
        request = urllib.request.Request(
            url,
            headers={
                "User-Agent": USER_AGENT,
                "Accept": "application/rss+xml, application/atom+xml, application/xml;q=0.9, */*;q=0.8",
                "Accept-Encoding": "gzip",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                body = response.read()
                if (response.headers.get("Content-Encoding") or "").lower() == "gzip":
                    body = gzip.decompress(body)
                return body, ""
        except urllib.error.HTTPError as exc:
            last_error = f"HTTP {exc.code} {exc.reason}"
            if exc.code in (429, 500, 502, 503, 504) and attempt < retries - 1:
                wait = _retry_after_seconds(exc.headers.get("Retry-After"))
                if wait is None:
                    wait = min(60.0, 5.0 * (2 ** attempt))
                time.sleep(wait)
                continue
            return None, last_error
        except Exception as exc:  # noqa: BLE001
            last_error = f"{type(exc).__name__}: {exc}"
            if attempt < retries - 1:
                time.sleep(min(30.0, 2.0 * (2 ** attempt)))
                continue
            return None, last_error
    return None, last_error


# --------------------------------------------------------------------------- #
# Feed parsing
# --------------------------------------------------------------------------- #

class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._parts: list[str] = []

    def handle_data(self, data: str) -> None:
        self._parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() in ("p", "br", "div", "li", "tr"):
            self._parts.append(" ")

    def text(self) -> str:
        return "".join(self._parts)


def html_to_text(raw: str) -> str:
    if not raw:
        return ""
    extractor = _TextExtractor()
    try:
        extractor.feed(raw)
        extractor.close()
        text = extractor.text()
    except Exception:  # noqa: BLE001 - malformed HTML must not kill the run
        text = re.sub(r"<[^>]+>", " ", raw)
    return re.sub(r"\s+", " ", text).strip()


def shorten(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    cut = text[:limit]
    for separator in ("。", "！", "？", ". ", "! ", "? ", "; ", "；", "，", ", ", " "):
        index = cut.rfind(separator)
        if index >= limit * 0.6:
            return cut[: index + len(separator)].rstrip() + "…"
    return cut.rstrip() + "…"


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].lower()


def _first_child_text(node: ET.Element, names: tuple[str, ...]) -> str:
    for child in node:
        if _local(child.tag) in names:
            text = (child.text or "").strip()
            if text:
                return text
    return ""


def _extract_link(node: ET.Element) -> str:
    for child in node:
        if _local(child.tag) != "link":
            continue
        href = (child.get("href") or "").strip()
        if href:
            rel = (child.get("rel") or "alternate").lower()
            if rel in ("alternate", ""):
                return href
            continue
        text = (child.text or "").strip()
        if text:
            return text
    for child in node:
        if _local(child.tag) in ("guid", "id"):
            text = (child.text or "").strip()
            if text.startswith("http"):
                return text
    return ""


def _parse_date(raw: str) -> datetime | None:
    raw = (raw or "").strip()
    if not raw:
        return None
    candidate = raw.replace("Z", "+00:00") if raw.endswith("Z") else raw
    try:
        parsed = datetime.fromisoformat(candidate)
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except ValueError:
        pass
    try:
        parsed = parsedate_to_datetime(raw)
        if parsed is not None:
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        pass
    for pattern in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d", "%d %b %Y %H:%M:%S"):
        try:
            return datetime.strptime(raw, pattern).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def parse_feed(body: bytes, source: Source, summary_limit: int) -> list[Entry]:
    root = None
    try:
        root = ET.fromstring(body)
    except ET.ParseError:
        # A few feeds ship a byte-order mark or stray control characters.
        text = body.decode("utf-8", "replace").lstrip("\ufeff")
        text = re.sub(r"^\s*<\?xml[^>]*\?>", "", text)
        text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", text)
        try:
            root = ET.fromstring(text)
        except ET.ParseError:
            return []

    nodes = [node for node in root.iter() if _local(node.tag) in ("item", "entry")]
    entries: list[Entry] = []
    for node in nodes:
        title = html_to_text(_first_child_text(node, ("title",)))
        link = _extract_link(node)
        if not title or not link:
            continue
        body_text = html_to_text(
            _first_child_text(node, ("description", "summary", "content", "encoded", "subtitle"))
        )
        published = _parse_date(
            _first_child_text(node, ("pubdate", "published", "updated", "date", "created", "issued"))
        )
        entries.append(
            Entry(
                title=re.sub(r"\s+", " ", title).strip(),
                link=link,
                summary=shorten(body_text, summary_limit),
                source_key=source.key,
                source_name=source.name,
                region=source.region,
                published=published,
            )
        )
    return entries


# --------------------------------------------------------------------------- #
# Selection / dedupe
# --------------------------------------------------------------------------- #

def resolve_window(now: datetime, since: str) -> tuple[datetime, str]:
    if since == "today":
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        return start, f"{start:%Y-%m-%d} 00:00 起（当日）"
    if since == "24h":
        return now - timedelta(hours=24), "最近 24 小时"
    if since == "48h":
        return now - timedelta(hours=48), "最近 48 小时"
    return now - timedelta(days=7), "最近 7 天"


def _title_key(title: str) -> str:
    return re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", title.lower())


def _link_key(link: str) -> str:
    parts = urlsplit(link.strip())
    path = parts.path.rstrip("/")
    return f"{parts.netloc.lower()}{path}".lower()


def select_entries(
    results: list[SourceResult], start: datetime, limit: int
) -> tuple[list[SourceResult], int, int]:
    """Filter by time window, dedupe across sources, cap items per source.

    Returns (results, kept_total, available_total) where available_total counts
    every entry inside the window before the per-source cap is applied.
    """
    seen_links: set[str] = set()
    seen_titles: set[str] = set()
    kept_total = 0
    available_total = 0
    output: list[SourceResult] = []

    for result in results:
        if result.error:
            output.append(result)
            continue
        fresh = [entry for entry in result.entries if entry.published is None or entry.published >= start]
        available_total += len(fresh)
        fresh.sort(key=lambda item: item.published.timestamp() if item.published else 0.0, reverse=True)
        kept: list[Entry] = []
        for entry in fresh:
            link_key = _link_key(entry.link)
            title_key = _title_key(entry.title)
            if link_key in seen_links or title_key in seen_titles:
                continue
            seen_links.add(link_key)
            seen_titles.add(title_key)
            kept.append(entry)
            if len(kept) >= limit:
                break
        kept_total += len(kept)
        output.append(SourceResult(source=result.source, entries=kept))
    return output, kept_total, available_total


# --------------------------------------------------------------------------- #
# Markdown output
# --------------------------------------------------------------------------- #

_MD_ESCAPE = re.compile(r"([\\`*_\[\]<>])")


def md_inline(text: str) -> str:
    return _MD_ESCAPE.sub(r"\\\1", text).replace("\n", " ")


def build_markdown(
    results: list[SourceResult],
    *,
    now: datetime,
    window_label: str,
    kept_total: int,
    ok_count: int,
    note: str,
) -> str:
    tz_label = now.strftime("%z")
    tz_label = f"UTC{tz_label[:3]}:{tz_label[3:]}" if tz_label else "本地时间"
    lines: list[str] = [
        f"# 全球新闻日报 · {now:%Y-%m-%d}",
        "",
        f"> 抓取时间：{now:%Y-%m-%d %H:%M:%S}（{tz_label}）",
        f"> 时间范围：{window_label}",
        f"> 新闻源：{len(results)} 个｜抓取成功 {ok_count}｜收录 {kept_total} 条（跨源去重后）",
        f"> 生成工具：{BOT_NAME} {BOT_VERSION}",
    ]
    if note:
        lines.append(f"> 说明：{note}")
    lines.append("")

    for result in results:
        if result.error:
            continue
        if not result.entries:
            continue
        lines.append(f"## {result.source.name}（{result.source.region}）")
        lines.append("")
        for index, entry in enumerate(result.entries, start=1):
            lines.append(f"{index}. **{md_inline(entry.title)}**")
            summary = md_inline(entry.summary) if entry.summary else "（该源未提供摘要）"
            lines.append(f"   - 摘要：{summary}")
            lines.append(f"   - 来源：{result.source.name}（{result.source.region}）")
            if entry.published:
                lines.append(f"   - 发布：{entry.published.astimezone():%Y-%m-%d %H:%M}")
            else:
                lines.append("   - 发布：该源未标注时间")
            lines.append(f"   - 链接：<{entry.link}>")
            lines.append("")

    if any(result.error for result in results):
        lines.append("## 抓取异常")
        lines.append("")
        for result in results:
            if result.error:
                lines.append(f"- {result.source.name}（{result.source.region}）：{result.error}")
        lines.append("")

    lines.append("---")
    lines.append("")
    lines.append("本文件由脚本自动生成，新闻内容版权归各原媒体所有，仅摘录标题、摘要与链接用于个人阅读。")
    lines.append("")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Locking
# --------------------------------------------------------------------------- #

class RunLock:
    """Prevents two scheduled runs from overlapping (e.g. cron + manual run)."""

    def __init__(self, path: Path, stale_minutes: int = 60) -> None:
        self.path = path
        self.stale_seconds = stale_minutes * 60
        self._held = False

    def acquire(self) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.exists():
            try:
                age = time.time() - self.path.stat().st_mtime
            except OSError:
                age = 0
            if age < self.stale_seconds:
                return False
            print(f"⚠ 发现过期锁文件（{int(age)} 秒前），已接管：{self.path}")
            try:
                self.path.unlink()
            except OSError:
                pass
        try:
            handle = os.open(str(self.path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(handle, str(os.getpid()).encode("utf-8"))
            os.close(handle)
            self._held = True
            return True
        except FileExistsError:
            return False

    def release(self) -> None:
        if self._held:
            try:
                self.path.unlink()
            except OSError:
                pass
            self._held = False


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="抓取当日全球新闻的公开 RSS 源，输出 Markdown 日报。",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--out", default="", help="输出 Markdown 路径，默认为 output/world-news-YYYY-MM-DD.md")
    parser.add_argument("--outdir", default="", help="只指定输出目录，文件名仍按日期自动生成")
    parser.add_argument("--since", default="today", choices=["today", "24h", "48h", "7d"], help="时间窗口")
    parser.add_argument("--limit", type=int, default=10, help="每个新闻源最多收录条数")
    parser.add_argument("--summary-limit", type=int, default=140, help="摘要最大字符数")
    parser.add_argument("--delay", type=float, default=2.0, help="同一站点两次请求的最小间隔（秒）")
    parser.add_argument("--max-per-minute", type=int, default=15, help="全局每分钟最大请求数")
    parser.add_argument("--timeout", type=float, default=15.0, help="单次请求超时（秒）")
    parser.add_argument("--retries", type=int, default=2, help="单个源的最大尝试次数")
    parser.add_argument("--proxy", default="", help="HTTP 代理地址，如 http://127.0.0.1:7890；不填则自动识别环境变量/Windows 系统代理")
    parser.add_argument("--no-proxy", action="store_true", help="强制直连，忽略任何代理设置")
    parser.add_argument("--sources", default="", help="只抓取指定源，逗号分隔的 key，如 bbc,guardian")
    parser.add_argument("--min-items", type=int, default=8, help="当日条目少于此数时自动放宽到 24 小时")
    parser.add_argument("--stdout", action="store_true", help="打印 Markdown 到终端而不写文件")
    parser.add_argument("--list-sources", action="store_true", help="列出内置新闻源")
    parser.add_argument("--no-lock", action="store_true", help="不使用运行锁")
    parser.add_argument("--quiet", action="store_true", help="只输出最终摘要")
    return parser.parse_args(argv)


def build_sources(selection: str) -> list[Source]:
    sources = [Source(**item) for item in DEFAULT_SOURCES]
    if not selection.strip():
        return sources
    wanted = {token.strip().lower() for token in selection.split(",") if token.strip()}
    chosen = [source for source in sources if source.key in wanted]
    unknown = wanted - {source.key for source in chosen}
    if unknown:
        print(f"⚠ 未知的源被忽略：{', '.join(sorted(unknown))}")
    return chosen


def _all_failed_hint() -> str:
    return (
        "⚠ 全部新闻源都抓取失败：先用 `--sources bbc --stdout` 单源排查；"
        "若网络无法直接访问境外站点，请先启动本地代理，或用 --proxy http://127.0.0.1:7890 指定。"
    )


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    enable_utf8_console()

    if args.list_sources:
        for source in build_sources(""):
            print(f"{source.key:<11} {source.name:<24} {source.region:<8} {source.url}")
        return 0

    sources = build_sources(args.sources)
    if not sources:
        print("没有可用的新闻源。", file=sys.stderr)
        return 2

    now = datetime.now().astimezone()
    if args.out:
        out_path = Path(args.out).expanduser()
    else:
        out_path = Path(args.outdir or "output").expanduser() / f"world-news-{now:%Y-%m-%d}.md"

    lock = RunLock(out_path.parent / ".crawl.lock")
    if not args.no_lock and not args.stdout:
        if not lock.acquire():
            print(f"⚠ 另一个抓取任务正在运行（{lock.path}），本次退出。", file=sys.stderr)
            return 3
    try:
        limiter = RateLimiter(args.delay, args.max_per_minute)
        robots = RobotsPolicy(limiter, min(args.timeout, 10.0))
        results: list[SourceResult] = []
        active_proxy, net_note = configure_network(args.proxy, args.no_proxy)
        if not args.quiet:
            print(f"开始抓取 {len(sources)} 个新闻源（间隔 {max(0.5, args.delay):.1f}s，每分钟上限 {args.max_per_minute} 次）")
            if active_proxy:
                print(f"使用代理：{active_proxy}")
            if net_note:
                print(f"⚠ {net_note}")

        for position, source in enumerate(sources, start=1):
            if not args.quiet:
                print(f"[{position}/{len(sources)}] {source.name} … ", end="", flush=True)
            if not robots.allowed(source.url):
                results.append(SourceResult(source=source, error="robots.txt 不允许抓取该地址"))
                if not args.quiet:
                    print("跳过（robots.txt）")
                continue
            body, error = http_get(source.url, args.timeout, args.retries, limiter)
            if body is None:
                results.append(SourceResult(source=source, error=error or "请求失败"))
                if not args.quiet:
                    print(f"失败（{error}）")
                continue
            entries = parse_feed(body, source, args.summary_limit)
            if not entries:
                results.append(SourceResult(source=source, error="解析不到任何新闻条目"))
                if not args.quiet:
                    print("失败（无条目）")
                continue
            results.append(SourceResult(source=source, entries=entries))
            if not args.quiet:
                print(f"OK，{len(entries)} 条")
    finally:
        if not args.no_lock and not args.stdout:
            lock.release()

    start, window_label = resolve_window(now, args.since)
    filtered, kept, available = select_entries(results, start, args.limit)
    note = ""
    if args.since == "today" and available < args.min_items and any(not item.error for item in results):
        widened_start, widened_label = resolve_window(now, "24h")
        filtered, kept, available = select_entries(results, widened_start, args.limit)
        window_label = f"{widened_label}（当日条目不足 {args.min_items} 条，自动放宽）"
        note = "当日已发布条目较少，已放宽到最近 24 小时以保证日报完整。"

    ok_count = sum(1 for result in filtered if not result.error)
    markdown = build_markdown(
        filtered,
        now=now,
        window_label=window_label,
        kept_total=kept,
        ok_count=ok_count,
        note=note,
    )

    if args.stdout:
        print(markdown)
        if not ok_count:
            print(_all_failed_hint(), file=sys.stderr)
        return 0 if ok_count else 2

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_bytes(markdown.encode("utf-8"))
    print(f"✅ 已写入 {out_path}（成功 {ok_count}/{len(filtered)} 个源，收录 {kept} 条）")
    for warning in robots.warnings:
        print(f"⚠ {warning}")
    if not ok_count:
        print(_all_failed_hint(), file=sys.stderr)
    return 0 if ok_count else 2


if __name__ == "__main__":
    sys.exit(main())
