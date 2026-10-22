"""Best-effort log-only counterfactuals; no foreground I/O or provider waits."""
from __future__ import annotations

import concurrent.futures as futures
from dataclasses import replace
import json
import os
from pathlib import Path
import queue
import re
import sys
import threading
import time
from urllib.parse import urlsplit, urlunsplit

OFF = {"0", "false", "off", "no"}
STATE = Path.home() / ".hermes/state/search-fusion"


def enabled(settings):
    return settings.shadow_enabled and os.getenv("SEARCH_FUSION_SHADOW", "1").lower() not in OFF


def safe_text(value, secrets=(), limit=300):
    text = str(value or "")
    for secret in secrets:
        if secret:
            text = text.replace(secret, "[REDACTED]")
    text = re.sub(r"(?i)(bearer\s+|(?:api[_-]?key|token|password|secret)\s*[=:]\s*)[^\s&]+", r"\1[REDACTED]", text)
    return text[:limit]


def safe_url(value, secrets=()):
    # Drop userinfo, query strings and fragments: they can contain signed tokens.
    try:
        parts = urlsplit(str(value))
        if parts.scheme not in {"http", "https"} or not parts.hostname:
            return ""
        return safe_text(urlunsplit((parts.scheme, parts.hostname, parts.path, "", "")), secrets, 300)
    except ValueError:
        return ""


class ShadowRunner:
    def __init__(self, state=STATE, queue_size=8, autostart=True):
        self.state = Path(state)
        self.queue = queue.Queue(maxsize=queue_size)
        # Separate from real search executor, plus a nonblocking submission bound:
        # tardy sockets cannot create an unbounded executor queue.
        self.pool = futures.ThreadPoolExecutor(max_workers=6, thread_name_prefix="fusion-shadow-provider")
        self.slots = threading.BoundedSemaphore(6)
        self.max_log_bytes = 5 * 1024 * 1024
        if autostart:
            self.worker = threading.Thread(target=self._worker, daemon=True, name="fusion-shadow")
            self.worker.start()

    def submit(self, provider, query, limit, settings, result, context):
        if not enabled(settings):
            return False
        # Bounded shallow value snapshot; never retain/mutate the result object.
        rows = [{k: row.get(k, "") for k in ("url", "title", "description")}
                for row in result["data"]["web"][:50]]
        job = (provider, query, limit, settings, rows, dict(context), time.time(),
               next((a.get("elapsed_ms", 0) for a in result.get("metadata", {}).get("fusion_attempts", []) if a.get("provider") == "serper"), 0))
        try:
            self.queue.put_nowait(job)
            return True
        except queue.Full:
            return False

    def _worker(self):
        while True:
            job = self.queue.get()
            if job is None:
                self.queue.task_done()
                return
            try:
                self.run(job)
            except Exception:
                # No exception bodies (which can contain credentials).
                pass
            finally:
                self.queue.task_done()

    def close(self):
        self.queue.join()
        if hasattr(self, "worker"):
            self.queue.put(None)
            self.worker.join(timeout=1)
        self.pool.shutdown(wait=True, cancel_futures=True)

    def _daily_claim(self, cap):
        # File lock makes the persisted UTC cap atomic across local processes.
        import fcntl
        self.state.mkdir(parents=True, exist_ok=True)
        with (self.state / "shadow-daily.lock").open("a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return False
            path = self.state / "shadow-daily.json"
            try:
                ledger = json.loads(path.read_text()) if path.exists() else {}
                if not isinstance(ledger, dict):
                    return False
            except ValueError:
                return False  # corrupt (including empty) accounting fails closed
            day = time.strftime("%Y-%m-%d", time.gmtime())
            count = ledger.get("count", 0) if ledger.get("day") == day else 0
            if not isinstance(count, int) or count < 0 or count >= cap:
                return False
            temporary = path.with_suffix(".tmp")
            with temporary.open("w") as stream:
                json.dump({"day": day, "count": count + 1}, stream)
                stream.flush()
                os.fsync(stream.fileno())
            temporary.replace(path)
            return True

    def run(self, job):
        provider, query, limit, settings, rows, context, ts, serper_ms = job
        f = sys.modules[type(provider).__module__]
        if not enabled(settings):
            return
        f._budget_allows("deepseek", charge=False)  # load current monthly ledger
        with f._BUDGET_LOCK:
            if f._BUDGET_CACHE.get("counts", {}).get("deepseek", 0) > .8 * f._MONTHLY_BUDGET["deepseek"]:
                return
        if not self._daily_claim(settings.shadow_daily_cap):
            return
        deadline = time.monotonic() + settings.shadow_timeout_s
        cancelled = threading.Event()
        outcomes = [{"provider": "serper", "status": "ok", "elapsed_ms": serper_ms, "rows": rows}]
        pending = {}
        settings = replace(settings, provider_timeout_s=min(settings.provider_timeout_s, settings.shadow_timeout_s))
        try:
            for name in f._PROVIDER_ORDER:
                if name == "serper":
                    continue
                status = None
                if time.monotonic() >= deadline:
                    status = "hard_deadline"
                elif name not in provider._searchers or not provider._configured(name):
                    status = "unavailable"
                elif name == "deepseek" and not f._deepseek_query_valid(query):
                    status = "unsupported_query"
                elif not self.slots.acquire(blocking=False):
                    status = "worker_busy"
                else:
                    lease, status = provider._coordinator.try_claim_shadow(name, settings)
                    if lease is None:
                        self.slots.release()
                    else:
                        try:
                            future = self.pool.submit(provider._invoke, name, provider._searchers[name],
                                                      query, limit, settings, lease, cancelled)
                        except Exception:
                            lease.finish("cancelled", settings)
                            self.slots.release()
                            status = "worker_submit_failed"
                        else:
                            future.add_done_callback(lambda _: self.slots.release())
                            pending[future] = (name, lease)
                if status:
                    outcomes.append({"provider": name, "status": status, "elapsed_ms": 0, "rows": []})
            done, unfinished = futures.wait(pending, timeout=max(0, deadline - time.monotonic())) if pending else (set(), set())
            for future in done:
                outcomes.append(future.result())
            for future in unfinished:
                name, lease = pending[future]
                outcomes.append({"provider": name, "status": "hard_deadline", "elapsed_ms": round(settings.shadow_timeout_s * 1000), "rows": []})
        finally:
            cancelled.set()
            for future, (_, lease) in pending.items():
                if future.cancel():
                    lease.finish("cancelled", settings)
        merged = provider._merge(outcomes, query, limit, settings.rrf_k)
        secrets = [f.get_provider_env(key) for key in ("DEEPSEEK_API_KEY", "SERPER_API_KEY", "TINYFISH_API_KEY", "BRAVE_SEARCH_API_KEY", "TAVILY_API_KEY", "EXA_API_KEY")]
        urls = lambda rr: sorted({u for r in rr if (u := safe_url(r["url"], secrets))})
        serper_urls, fusion_urls = urls(rows), urls(merged)
        record = {"ts": ts, "session_id": safe_text(context.get("session_id"), secrets),
                  "turn_id": safe_text(context.get("turn_id"), secrets),
                  "batch_index": context.get("batch_index"), "query": safe_text(query, secrets, 2048),
                  "serper_urls": serper_urls, "fusion_urls": fusion_urls,
                  "fusion_only_urls": sorted(set(fusion_urls) - set(serper_urls)),
                  "providers_used": [o["provider"] for o in outcomes if o["status"] == "ok"],
                  "providers": [{k: o[k] for k in ("provider", "status", "elapsed_ms")} for o in outcomes],
                  "results": [{"url": safe_url(r["url"], secrets), "title": safe_text(r["title"], secrets),
                               "snippet": safe_text(r["description"], secrets)} for r in merged]}
        self._append(record)

    def _append(self, record):
        import fcntl
        self.state.mkdir(parents=True, exist_ok=True)
        line = json.dumps(record, ensure_ascii=False) + "\n"
        with (self.state / "shadow-log.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            path = self.state / "shadow.jsonl"
            if path.exists() and path.stat().st_size + len(line.encode()) > self.max_log_bytes:
                path.replace(self.state / "shadow.jsonl.1")
            with path.open("a") as stream:
                stream.write(line)


SHADOW = ShadowRunner()
