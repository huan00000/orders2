"""记录准备发送的 HTTP 请求；所有联动步骤复用此会话。"""
import json
import os
import re
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

LOG_PATH = Path(os.environ.get("DATA_DIR", Path(__file__).resolve().parent)) / "logs.md"
RETENTION = timedelta(weeks=4)
ENTRY_HEADER = re.compile(r"^### (\d{4}-\d{2}-\d{2}T\S+) · ")
SENSITIVE = {"key", "sign", "authorization", "proxy-authorization", "cookie",
             "set-cookie", "api_key", "api_secret", "secret", "token", "password"}


class LoggedSession(requests.Session):
    def __init__(self, log_path=LOG_PATH):
        super().__init__()
        self.log_path = Path(log_path)
        self.stage = "初始化"
        self.request_count = 0
        self._secrets = set()
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        self._next_cleanup = 0
        self.prune()

    def prune(self, now=None):
        """流式清除超过 28 天的完整条目，保留说明文字及代码块内容。"""
        now = now or datetime.now(timezone.utc)
        cutoff = now - RETENTION
        if not self.log_path.exists():
            return
        temporary = None
        try:
            with self.log_path.open(encoding="utf-8") as source, tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", dir=self.log_path.parent,
                prefix=".logs-", suffix=".tmp", delete=False,
            ) as target:
                temporary = Path(target.name)
                keep, fence = True, None
                for line in source:
                    stripped = line.strip()
                    if fence is None:
                        match = ENTRY_HEADER.match(line)
                        if match:
                            stamp = datetime.fromisoformat(match[1])
                            if stamp.tzinfo is None:
                                stamp = stamp.astimezone()
                            keep = stamp >= cutoff
                        if re.fullmatch(r"`{3,}", stripped):
                            fence = stripped
                    elif stripped == fence:
                        fence = None
                    if keep:
                        target.write(line)
            os.replace(temporary, self.log_path)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
        self._next_cleanup = time.monotonic() + 60

    def _clean(self, value):
        if isinstance(value, dict):
            return {key: "[已脱敏]" if str(key).lower() in SENSITIVE else self._clean(item)
                    for key, item in value.items()}
        if isinstance(value, (list, tuple)):
            return [self._clean(item) for item in value]
        if isinstance(value, bytes):
            value = value.decode("utf-8", errors="replace")
        if isinstance(value, str):
            for secret in sorted(self._secrets, key=len, reverse=True):
                value = value.replace(secret, "[已脱敏]")
        return value

    def note(self, title, content):
        if time.monotonic() >= self._next_cleanup:
            self.prune()
        content = self._clean(content)
        if not isinstance(content, str):
            content = json.dumps(content, ensure_ascii=False, indent=2)
        fence = "`" * max(3, max((len(m[0]) + 1 for m in re.finditer(r"`+", content)), default=3))
        stamp = datetime.now().astimezone().isoformat(timespec="seconds")
        with self.log_path.open("a", encoding="utf-8") as log:
            log.write(f"\n### {stamp} · {title}\n\n{fence}\n{content}\n{fence}\n")

    def send(self, request, **kwargs):
        if kwargs.get("timeout") is None:
            kwargs["timeout"] = 20
        for key, value in request.headers.items():
            if key.lower() in SENSITIVE and value:
                self._secrets.add(str(value))
        self.request_count += 1
        request_id = self.request_count
        body = request.body
        if body:
            try:
                body = json.loads(body)
            except (ValueError, TypeError):
                pass
        self.note(f"📤 请求 #{request_id} · {self.stage}", {
            "method": request.method, "url": request.url,
            "headers": dict(request.headers), "body": body,
            "timeout_seconds": kwargs.get("timeout"),
        })
        start = time.monotonic()
        try:
            response = super().send(request, **kwargs)
        except requests.RequestException as exc:
            self.note(f"❌ 请求 #{request_id} · 网络失败", f"{type(exc).__name__}: {exc}")
            raise
        try:
            result = response.json()
        except ValueError:
            result = response.text
        icon = "✅" if response.ok else "❌"
        self.note(f"{icon} 响应 #{request_id} · HTTP {response.status_code}", {
            "elapsed_ms": round((time.monotonic() - start) * 1000), "body": result,
        })
        return response
