"""
Ableton Link optional-import block.

Tries link-python first (synchronous), then aalink (asyncio-based).
Exports: LINK_AVAILABLE, _link, _link_api, _link_loop,
         _lnk_peers(), _lnk_get_tempo(), _lnk_get_beat()
"""

import asyncio
import threading

_link      = None
_link_api  = None   # "link" | "aalink" | None
_link_loop = None   # background asyncio loop used by aalink
LINK_AVAILABLE = False

# link-python is synchronous — no event loop needed
try:
    import importlib as _il
    _lm = _il.import_module("link")
    _link = _lm.Link(120.0)
    try:
        _link.enabled = True
    except AttributeError:
        pass
    _link_api = "link"
    LINK_AVAILABLE = True
except Exception as _e:
    print(f"[Link] link-python: {_e}")

# aalink requires a *running* asyncio event loop even for construction,
# so we spin one up in a daemon thread and create Link inside it.
if not LINK_AVAILABLE:
    try:
        import aalink as _aalink_mod
        _link_loop = asyncio.new_event_loop()
        threading.Thread(target=_link_loop.run_forever,
                         daemon=True, name="aalink-loop").start()

        async def _mk_aalink():
            lnk = _aalink_mod.Link(120.0)
            try:
                lnk.enabled = True
            except AttributeError:
                pass
            return lnk

        _link = asyncio.run_coroutine_threadsafe(
            _mk_aalink(), _link_loop).result(timeout=5.0)
        _link_api = "aalink"
        LINK_AVAILABLE = True
        print("[Link] aalink loaded OK")
    except Exception as _e:
        import traceback as _tb
        print(f"[Link] aalink: {_e}")
        _tb.print_exc()

def _lnk_peers() -> int:
    if _link_api == "aalink":
        return int(_link.num_peers)         # type: ignore[union-attr]
    return int(_link.numPeers())            # type: ignore[union-attr]

def _lnk_get_tempo() -> float:
    """Return current tempo in BPM — works for both aalink and link-python."""
    if _link_api == "aalink":
        return float(_link.tempo)           # type: ignore[union-attr]
    state = _link.captureSessionState()     # type: ignore[union-attr]
    return float(state.tempo())

def _lnk_get_beat() -> float:
    """Return current beat counter — works for both aalink and link-python."""
    if _link_api == "aalink":
        return float(_link.beat)            # type: ignore[union-attr]
    state  = _link.captureSessionState()    # type: ignore[union-attr]
    micros = _link.clock().micros()         # type: ignore[union-attr]
    return float(state.beatAtTime(micros, 4.0))
