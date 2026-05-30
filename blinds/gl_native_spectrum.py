"""
Native OpenGL spectrum panel — bypasses the tkinter PhotoImage upload.

Architecture
------------
- A GLFW window is created with NO decorations and immediately re-parented
  to a tkinter Frame's HWND via Win32 ``SetParent`` so it lives inside the
  Tk window like any other widget.
- moderngl renders the spectrum directly into that window's swap chain.
  No ``fbo.read()`` → no ``PIL.Image.frombytes()`` → no ``PhotoImage.paste()``.
  That saves ~7-10 ms per frame on the user's setup (PhotoImage was the
  dominant cost of the previous bitmap path).
- Render is driven from the Tk main thread via ``tk.after(16, render)``.
  GLFW's API requires its window functions to be called from the main
  thread, so we don't use a worker — Tk's event loop pumps the renders.
- Shader source + colour gradient are imported from ``gpu_spectrum`` so
  there is exactly one canonical definition of the spectrum look.

Falls back gracefully: if glfw, moderngl, or Win32 reparenting isn't
available, ``try_create()`` returns ``None`` and the caller can keep
using the existing PhotoImage path.
"""

from __future__ import annotations

import ctypes
import logging
from typing import Any, Optional, Tuple

import numpy as np

log = logging.getLogger("blinds")

try:
    import glfw
    _GLFW_OK = True
except ImportError:
    glfw = None  # type: ignore
    _GLFW_OK = False

try:
    import moderngl
    _MGL_OK = True
except ImportError:
    moderngl = None  # type: ignore
    _MGL_OK = False


# ── Win32 API setup (Windows only) ──────────────────────────────────────────
_user32: Any = None
if hasattr(ctypes, "windll"):
    try:
        _user32 = ctypes.windll.user32
        _LONG_PTR = ctypes.c_int64 if ctypes.sizeof(ctypes.c_void_p) == 8 \
                    else ctypes.c_long
        _user32.SetParent.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        _user32.SetParent.restype  = ctypes.c_void_p
        _user32.SetWindowLongPtrW.argtypes = [ctypes.c_void_p, ctypes.c_int, _LONG_PTR]
        _user32.SetWindowLongPtrW.restype  = _LONG_PTR
        _user32.GetWindowLongPtrW.argtypes = [ctypes.c_void_p, ctypes.c_int]
        _user32.GetWindowLongPtrW.restype  = _LONG_PTR
        _user32.SetWindowPos.argtypes = [
            ctypes.c_void_p, ctypes.c_void_p,
            ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
            ctypes.c_uint
        ]
        _user32.SetWindowPos.restype = ctypes.c_int
    except Exception:
        _user32 = None

GWL_STYLE       = -16
WS_CHILD        = 0x40000000
WS_POPUP        = 0x80000000
WS_VISIBLE      = 0x10000000
WS_CAPTION      = 0x00C00000
WS_THICKFRAME   = 0x00040000
WS_OVERLAPPED   = 0x00000000
SWP_NOZORDER    = 0x0004
SWP_NOACTIVATE  = 0x0010


class NativeGLSpectrum:
    """A glfw + moderngl spectrum panel embedded inside a tkinter Frame."""

    def __init__(self, parent_widget, width: int = 800, height: int = 200):
        if not _GLFW_OK:
            raise RuntimeError("glfw not installed (pip install glfw)")
        if not _MGL_OK:
            raise RuntimeError("moderngl not installed (pip install moderngl)")
        if _user32 is None:
            raise RuntimeError("Win32 user32 unavailable — embedded GL only "
                                "supported on Windows for now")
        # Lazy-import the shader source so this module compiles even without
        # the rest of the GL stack in place.
        from .gpu_spectrum import _VERT, _FRAG, _build_color_gradient

        self._parent_widget = parent_widget
        parent_widget.update_idletasks()
        self._parent_hwnd = int(parent_widget.winfo_id())
        self._width  = max(16, int(width))
        self._height = max(16, int(height))

        # ── GLFW window creation (must run on main thread) ──────────────
        if not glfw.init():
            raise RuntimeError("glfw.init() failed")
        glfw.window_hint(glfw.VISIBLE,             glfw.FALSE)
        glfw.window_hint(glfw.DECORATED,           glfw.FALSE)
        glfw.window_hint(glfw.FOCUSED,             glfw.FALSE)
        glfw.window_hint(glfw.FOCUS_ON_SHOW,       glfw.FALSE)
        glfw.window_hint(glfw.CONTEXT_VERSION_MAJOR, 3)
        glfw.window_hint(glfw.CONTEXT_VERSION_MINOR, 3)
        glfw.window_hint(glfw.OPENGL_PROFILE,       glfw.OPENGL_CORE_PROFILE)
        self._window = glfw.create_window(
            self._width, self._height, "spectrum", None, None)
        if not self._window:
            glfw.terminate()
            raise RuntimeError("glfw.create_window() failed")

        # ── Re-parent to the tkinter frame (Win32) ──────────────────────
        self._hwnd = glfw.get_win32_window(self._window)
        _user32.SetParent(self._hwnd, self._parent_hwnd)
        style = _user32.GetWindowLongPtrW(self._hwnd, GWL_STYLE)
        style = ((style & ~WS_POPUP & ~WS_CAPTION & ~WS_THICKFRAME)
                  | WS_CHILD | WS_VISIBLE)
        _user32.SetWindowLongPtrW(self._hwnd, GWL_STYLE, style)
        _user32.SetWindowPos(self._hwnd, None,
                              0, 0, self._width, self._height,
                              SWP_NOZORDER | SWP_NOACTIVATE)
        glfw.show_window(self._window)

        # ── GL context + shader (reused from gpu_spectrum) ──────────────
        glfw.make_context_current(self._window)
        glfw.swap_interval(1)   # vsync — caps at monitor refresh rate
        self._ctx = moderngl.create_context()
        log.info("NativeGLSpectrum: %s (%s)",
                 self._ctx.info.get("GL_RENDERER", "?"),
                 self._ctx.info.get("GL_VERSION",  "?"))

        self._prog = self._ctx.program(vertex_shader=_VERT, fragment_shader=_FRAG)
        self._prog["curve_tex"]  = 0
        self._prog["color_grad"] = 1

        # Same Y-flipped quad as the offscreen renderer
        quad = np.array([
            -1.0, -1.0, 0.0, 1.0,
             1.0, -1.0, 1.0, 1.0,
            -1.0,  1.0, 0.0, 0.0,
             1.0,  1.0, 1.0, 0.0,
        ], dtype=np.float32)
        self._vbo = self._ctx.buffer(quad.tobytes())
        self._vao = self._ctx.vertex_array(self._prog, [
            (self._vbo, "2f 2f", "in_pos", "in_uv"),
        ])

        # Static color gradient texture
        grad = _build_color_gradient(256)
        self._color_grad_tex = self._ctx.texture(
            (256, 1), 3, dtype="f4", data=grad.tobytes())
        self._color_grad_tex.repeat_x = False
        self._color_grad_tex.repeat_y = False
        self._color_grad_tex.filter = (moderngl.LINEAR, moderngl.LINEAR)

        # Curve texture (resized on demand)
        self._curve_tex: Any = None
        self._curve_tex_w = 0

        # Latest published data — no lock; publish and render are both on
        # the Tk main thread.
        self._curve: np.ndarray   = np.zeros(self._width, dtype=np.float32)
        self._kick: Tuple[float, float] = (0.0, 0.0)
        self._hh:   Tuple[float, float] = (0.0, 0.0)

        self._alive = True

    def set_geometry(self, width: int, height: int) -> None:
        """Resize the embedded child window to match the parent's
        current dimensions.  Called from a ``<Configure>`` binding on
        the tkinter Frame."""
        if not self._alive:
            return
        if width < 16 or height < 16:
            return
        if width == self._width and height == self._height:
            return
        self._width  = int(width)
        self._height = int(height)
        _user32.SetWindowPos(self._hwnd, None,
                              0, 0, self._width, self._height,
                              SWP_NOZORDER | SWP_NOACTIVATE)
        try:
            glfw.set_window_size(self._window, self._width, self._height)
        except Exception:
            pass

    def publish(self,
                curve_heights: np.ndarray,
                kick_range:    Tuple[float, float],
                hh_range:      Tuple[float, float]) -> None:
        """Latch the latest spectrum data for the next ``render()`` call.
        Cheap — no GL work happens here."""
        self._curve = curve_heights
        self._kick  = kick_range
        self._hh    = hh_range

    def render(self) -> None:
        """Render one frame to the embedded swap chain.  Must be called
        from the Tk main thread (GLFW requirement)."""
        if not self._alive:
            return
        glfw.make_context_current(self._window)
        cw = self._width
        ch = self._height

        # Resize curve texture to match canvas width
        if self._curve_tex_w != cw or self._curve_tex is None:
            if self._curve_tex is not None:
                self._curve_tex.release()
            self._curve_tex = self._ctx.texture((cw, 1), 1, dtype="f4")
            self._curve_tex.filter   = (moderngl.NEAREST, moderngl.NEAREST)
            self._curve_tex.repeat_x = False
            self._curve_tex.repeat_y = False
            self._curve_tex_w = cw

        # If the published curve length doesn't match the canvas, resample.
        curve = self._curve
        if curve.shape[0] != cw:
            curve = np.interp(np.arange(cw),
                              np.linspace(0, cw - 1, len(curve)),
                              curve).astype(np.float32)
        self._curve_tex.write(curve.astype(np.float32, copy=False).tobytes())

        self._curve_tex.use(location=0)
        self._color_grad_tex.use(location=1)

        self._prog["canvas_size"] = (float(cw), float(ch))
        self._prog["kick_range"]  = (float(self._kick[0]), float(self._kick[1]))
        self._prog["hh_range"]    = (float(self._hh[0]),   float(self._hh[1]))

        # Render to default framebuffer (the swap chain)
        self._ctx.screen.use()
        self._ctx.viewport = (0, 0, cw, ch)
        self._ctx.clear(0.05, 0.05, 0.09, 1.0)
        self._vao.render(moderngl.TRIANGLE_STRIP)

        glfw.swap_buffers(self._window)
        # Poll events so the child window stays responsive — but we don't
        # actually expect any input events (focus is disabled).
        glfw.poll_events()

    def stop(self) -> None:
        if not self._alive:
            return
        self._alive = False
        for r in (self._curve_tex, self._color_grad_tex,
                   self._vao, self._vbo, self._prog):
            try:
                if r is not None:
                    r.release()
            except Exception:
                pass
        try:
            if self._ctx is not None:
                self._ctx.release()
        except Exception:
            pass
        try:
            if self._window is not None:
                glfw.destroy_window(self._window)
        except Exception:
            pass
        # NOTE: don't call glfw.terminate() here — other glfw windows in
        # the same process (none today, but a fallback path might add some)
        # would be invalidated.  glfw cleans up at interpreter exit.


def try_create(parent_widget, width: int, height: int) -> Optional[NativeGLSpectrum]:
    """Return a NativeGLSpectrum or None if any dependency is missing."""
    if not _GLFW_OK:
        log.info("Native GL spectrum unavailable: glfw not installed")
        return None
    if not _MGL_OK:
        log.info("Native GL spectrum unavailable: moderngl not installed")
        return None
    if _user32 is None:
        log.info("Native GL spectrum unavailable: Win32-only path")
        return None
    try:
        return NativeGLSpectrum(parent_widget, width, height)
    except Exception as exc:
        log.warning("NativeGLSpectrum init failed (%s); CPU fallback in use",
                    exc, exc_info=False)
        return None
