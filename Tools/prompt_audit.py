import atexit
import hashlib
import json
import os
import queue
import re
import threading
import time
from typing import Any, Dict, List, Optional


_DEFAULT_DIR = os.path.normpath(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "log"))
_KNOWLEDGE_MARKER = "【知识库检索结果】"

_SECRET_PATTERNS: List[re.Pattern] = [
    re.compile(r"(Bearer)\s+([A-Za-z0-9_\-\.=]{12,})", re.IGNORECASE),
    re.compile(r"(\bapi[_-]?key\b)\s*[:=]\s*([^\s\"']{8,})", re.IGNORECASE),
    re.compile(r"(\bAuthorization\b)\s*[:=]\s*([^\n]{8,})", re.IGNORECASE),
    re.compile(r"\bsk-[A-Za-z0-9]{16,}\b"),
]


def _now_ts() -> int:
    return int(time.time())


def _now_iso() -> str:
    try:
        return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime())
    except Exception:
        return str(_now_ts())


def _safe_trim(text: str, max_chars: int) -> str:
    s = str(text or "")
    if max_chars <= 0:
        return s
    if len(s) <= max_chars:
        return s
    return s[:max_chars] + f"...(truncated,{len(s)}chars)"


def _redact_knowledge_block(text: str) -> str:
    s = str(text or "")
    if not s:
        return ""
    idx = s.find(_KNOWLEDGE_MARKER)
    if idx < 0:
        return s
    head = s[:idx].rstrip()
    tail = s[idx + len(_KNOWLEDGE_MARKER) :].lstrip("\n")
    sha1 = hashlib.sha1(tail.encode("utf-8", errors="ignore")).hexdigest()
    return f"{head}\n\n{_KNOWLEDGE_MARKER}\n[REDACTED len={len(tail)} sha1={sha1}]"


def _redact_text(text: str) -> str:
    if not text:
        return ""
    out = str(text)
    for p in _SECRET_PATTERNS:
        out = p.sub(lambda m: f"{m.group(1)} [REDACTED]" if m.lastindex and m.lastindex >= 1 else "[REDACTED]", out)
    out = re.sub(r"([?&]key=)([^&\s]+)", r"\1[REDACTED]", out, flags=re.IGNORECASE)
    return out


def _sanitize_text(text: str) -> str:
    return _redact_text(_redact_knowledge_block(text))


class PromptAuditConfig:
    def __init__(self, d: Optional[Dict[str, Any]] = None) -> None:
        d = d if isinstance(d, dict) else {}
        self.enabled = bool(d.get("enabled", False))
        self.echo_console = bool(d.get("echo_console", False))
        self.include_history = bool(d.get("include_history", True))
        self.include_images = bool(d.get("include_images", True))
        self.flush_interval_seconds = int(d.get("flush_interval_seconds", 10))
        self.max_entry_chars = int(d.get("max_entry_chars", 120000))
        self.max_file_mb = int(d.get("max_file_mb", 20))
        self.max_queue = int(d.get("max_queue", 5000))
        self.dir = os.path.normpath(str(d.get("dir") or _DEFAULT_DIR))

        self.flush_interval_seconds = max(1, self.flush_interval_seconds)
        self.max_entry_chars = max(2000, self.max_entry_chars)
        self.max_file_mb = max(1, self.max_file_mb)
        self.max_queue = max(100, self.max_queue)


class PromptAudit:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._cfg = PromptAuditConfig({})
        self._q: "queue.Queue[Dict[str, Any]]" = queue.Queue(maxsize=5000)
        self._t: Optional[threading.Thread] = None
        self._stop = threading.Event()

    def configure(self, cfg: PromptAuditConfig) -> None:
        with self._lock:
            self._cfg = cfg
            self._q = queue.Queue(maxsize=int(cfg.max_queue))
            if cfg.enabled and self._t is None:
                self._start_locked()

    def _start_locked(self) -> None:
        if self._t is not None:
            return
        self._stop.clear()
        self._t = threading.Thread(target=self._loop, name="prompt_audit_writer", daemon=True)
        self._t.start()

    def stop(self) -> None:
        self._stop.set()
        t = None
        with self._lock:
            t = self._t
            self._t = None
        if t is not None:
            try:
                t.join(timeout=2)
            except Exception:
                pass

    def _ensure_file(self) -> str:
        with self._lock:
            cfg = self._cfg
        os.makedirs(cfg.dir, exist_ok=True)
        date = time.strftime("%Y%m%d", time.localtime())
        path = os.path.join(cfg.dir, f"prompt_audit_{date}.jsonl")
        return os.path.normpath(path)

    def _should_rotate(self, path: str) -> bool:
        with self._lock:
            cfg = self._cfg
        try:
            if not os.path.exists(path):
                return False
            size = os.path.getsize(path)
            return size >= int(cfg.max_file_mb) * 1024 * 1024
        except Exception:
            return False

    def _rotate(self, path: str) -> str:
        try:
            ts = time.strftime("%Y%m%d_%H%M%S", time.localtime())
            dst = os.path.join(os.path.dirname(path), f"prompt_audit_{ts}.jsonl")
            if os.path.exists(path):
                os.replace(path, dst)
        except Exception:
            pass
        return self._ensure_file()

    def _flush_batch(self, batch: List[Dict[str, Any]]) -> None:
        if not batch:
            return
        path = self._ensure_file()
        if self._should_rotate(path):
            path = self._rotate(path)
        try:
            with open(path, "a", encoding="utf-8") as f:
                for e in batch:
                    f.write(json.dumps(e, ensure_ascii=False))
                    f.write("\n")
        except Exception:
            pass

    def log_chat_call(
        self,
        config_name: str,
        uid: Any,
        sys_prompt: str,
        user_message: str,
        history: Optional[List[Dict[str, str]]] = None,
        images: Optional[List[str]] = None,
        extra: Optional[Dict[str, Any]] = None,
    ) -> None:
        with self._lock:
            cfg = self._cfg
            if not cfg.enabled:
                return
            if self._t is None:
                self._start_locked()

        entry: Dict[str, Any] = {
            "ts": _now_ts(),
            "time": _now_iso(),
            "kind": "chat",
            "config": str(config_name),
            "uid": str(uid),
            "system": _safe_trim(_sanitize_text(sys_prompt), cfg.max_entry_chars),
            "user": _safe_trim(_sanitize_text(user_message), cfg.max_entry_chars),
        }
        if cfg.include_history and history is not None:
            safe_hist: List[Dict[str, str]] = []
            for h in history:
                if not isinstance(h, dict):
                    continue
                role = str(h.get("role") or "")
                content = _sanitize_text(str(h.get("content") or ""))
                safe_hist.append({"role": role, "content": _safe_trim(content, cfg.max_entry_chars)})
            entry["history"] = safe_hist
        if cfg.include_images and images is not None:
            entry["images"] = [_safe_trim(_sanitize_text(str(x)), 2000) for x in images]
        if isinstance(extra, dict) and extra:
            entry["extra"] = extra

        try:
            self._q.put_nowait(entry)
        except queue.Full:
            return

        if cfg.echo_console:
            try:
                from Tools.log_helper import project_log

                project_log("[prompt_audit]", json.dumps(entry, ensure_ascii=False))
            except Exception:
                pass

    def _loop(self) -> None:
        while not self._stop.is_set():
            with self._lock:
                cfg = self._cfg
            if not cfg.enabled:
                time.sleep(0.2)
                continue
            batch: List[Dict[str, Any]] = []
            started = _now_ts()
            while len(batch) < 50 and (_now_ts() - started) < cfg.flush_interval_seconds:
                try:
                    item = self._q.get(timeout=0.2)
                    if isinstance(item, dict):
                        batch.append(item)
                except queue.Empty:
                    if self._stop.is_set():
                        break
                    continue
            self._flush_batch(batch)

        try:
            rest: List[Dict[str, Any]] = []
            while True:
                item = self._q.get_nowait()
                if isinstance(item, dict):
                    rest.append(item)
                if len(rest) >= 200:
                    self._flush_batch(rest)
                    rest = []
        except Exception:
            pass


_AUDIT = PromptAudit()


def configure_from_dict(d: Optional[Dict[str, Any]]) -> None:
    _AUDIT.configure(PromptAuditConfig(d))


def log_chat_call(
    config_name: str,
    uid: Any,
    sys_prompt: str,
    user_message: str,
    history: Optional[List[Dict[str, str]]] = None,
    images: Optional[List[str]] = None,
    extra: Optional[Dict[str, Any]] = None,
) -> None:
    _AUDIT.log_chat_call(
        config_name=config_name,
        uid=uid,
        sys_prompt=sys_prompt,
        user_message=user_message,
        history=history,
        images=images,
        extra=extra,
    )


def stop() -> None:
    _AUDIT.stop()


atexit.register(stop)

