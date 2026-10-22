"""Plugin-owned turn/batch routing; no core patches or provider admission changes."""
from __future__ import annotations

from collections import OrderedDict
from contextvars import ContextVar
from dataclasses import dataclass, field
import threading
import time

# Public pre_tool_call hook identities, propagated through context-aware workers.
_CONTEXT: ContextVar[tuple[str, str, str]] = ContextVar("fusion_call_context", default=("", "", ""))


# Hook callbacks may run on bounded worker threads: ContextVar writes there do
# not flow back to the caller. Bridge via tool correlation IDs, never thread ID.
_CALLS: OrderedDict[tuple[str, str, str], tuple[str, str, str]] = OrderedDict()
_CALL_LOCK = threading.Lock()


def capture_call_context(*, session_id="", turn_id="", api_request_id="", tool_call_id="", **kwargs):
    context = (str(session_id or ""), str(turn_id or ""), str(api_request_id or ""))
    if session_id and tool_call_id:
        with _CALL_LOCK:
            _CALLS[(session_id, turn_id, tool_call_id)] = context
            while len(_CALLS) > 4096:
                _CALLS.popitem(last=False)
    # Also supports hosts running hooks inline and standalone callers.
    _CONTEXT.set(context)


def clear_call_context(*, session_id="", turn_id="", tool_call_id="", **kwargs):
    with _CALL_LOCK:
        _CALLS.pop((session_id, turn_id, tool_call_id), None)
    _CONTEXT.set(("", "", ""))


def current_context():
    # Narrow, optional host seam. These contextvars are bound around provider
    # execution by model_tools._approval_observability in this host.
    try:
        from tools import approval_context as ac
        key = (ac._approval_session_id.get(), ac._approval_turn_id.get(), ac._approval_tool_call_id.get())
    except (ImportError, AttributeError):
        key = ("", "", "")
    if key[0]:
        with _CALL_LOCK:
            captured = _CALLS.get(key)
        return captured or (key[0], key[1], "")
    context = _CONTEXT.get()
    if context[0]:
        return context
    try:
        from gateway.session_context import get_session_env
        session = get_session_env("HERMES_SESSION_ID", "") or get_session_env("HERMES_SESSION_KEY", "")
    except (ImportError, AttributeError):
        session = ""
    return (session, "", "")


@dataclass
class _Turn:
    first_batch: str
    first_at: float
    last_at: float
    batches: dict[str, int] = field(default_factory=dict)


class TurnRouter:
    """Atomic first-search-batch selection, shared across provider instances."""

    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self.lock = threading.Lock()
        self.turns: OrderedDict[tuple[str, str], _Turn] = OrderedDict()

    def select(self, *, enabled=True, idle_gap_s=120.0, batch_window_s=1.0):
        if not enabled:
            return "fusion", "disabled"
        session, turn, batch = current_context()
        if not session:
            # Never collapse unrelated unidentified callers into a global session.
            return "fusion", "no_session"
        now = self.clock()
        key = (session, turn)
        with self.lock:
            state = self.turns.get(key)
            if state is None or (not turn and now - state.last_at >= idle_gap_s):
                state = _Turn(batch, now, now)
                self.turns[key] = state
            state.last_at = now
            self.turns.move_to_end(key)
            while len(self.turns) > 4096:
                self.turns.popitem(last=False)
            batch_key = batch or str(int((now - state.first_at) / max(batch_window_s, .001)))
            if batch_key not in state.batches:
                if len(state.batches) >= 256:
                    state.batches.pop(next(iter(state.batches)))
                state.batches[batch_key] = max(state.batches.values(), default=0) + 1
            _DECISION.set({"session_id": session, "turn_id": turn,
                           "batch_index": state.batches[batch_key]})
            if batch and state.first_batch:
                first = batch == state.first_batch
            else:
                first = now - state.first_at <= batch_window_s
        boundary = "hook_ids" if turn and batch else "turn_id_batch_window" if turn else "session_idle_gap"
        return ("fusion" if first else "serper"), boundary


_DECISION: ContextVar[dict] = ContextVar("fusion_decision", default={})


def decision_context():
    return dict(_DECISION.get())


ROUTER = TurnRouter()
