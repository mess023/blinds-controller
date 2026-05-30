"""
GPU-rendered spectrum analyzer using moderngl.

Renders to a hidden offscreen framebuffer via a fragment shader, then reads
the pixels back as a numpy array.  The caller pushes those pixels into the
existing PIL → ImageTk → tkinter Canvas pipeline.

Why offscreen + readback rather than a native GL window? Because tkinter has
no GL widget on Windows, embedding a separate OS window is fragile, and the
readback path is plenty fast (RTX 2060 → CPU: <2 ms for a 1200×400 RGB grab).

Visual win over the CPU bitmap path:
- True per-pixel work: gradients, glow, anti-aliased outline
- Smooth animation: the GPU draws in <0.5 ms; framerate is bound only by the
  readback + PIL paste, not by polygon/canvas-item overhead

Requires:
    pip install moderngl
"""

from __future__ import annotations

import logging
import math
from typing import Any, Optional, Tuple

import numpy as np

log = logging.getLogger("blinds")

try:
    import moderngl
    _MGL_OK = True
except ImportError:
    moderngl = None  # type: ignore
    _MGL_OK = False


_VERT = """
#version 330
in vec2 in_pos;
in vec2 in_uv;
out vec2 uv;
void main() {
    gl_Position = vec4(in_pos, 0.0, 1.0);
    uv = in_uv;
}
"""

# Fragment shader: per-pixel render of the spectrum.
#
# Inputs:
#   curve_tex   — 1-D texture (W × 1, R32F), the bar-top height per column
#                 in [0, 1] where 0 = no bar, 1 = full canvas height
#   color_grad  — 1-D texture (256 × 1, RGB32F), the freq→color gradient
#   canvas_size — pixel dimensions
#   kick_range  — (x0, x1) normalised in [0, 1]
#   hh_range    — same for the hi-hat band
_FRAG = """
#version 330

in vec2 uv;
out vec4 frag_color;

uniform sampler2D curve_tex;
uniform sampler2D color_grad;
uniform vec2 canvas_size;
uniform vec2 kick_range;
uniform vec2 hh_range;

void main() {
    // uv.x: 0..1 left→right. uv.y: 0..1 bottom→top (GL convention).
    float x = uv.x;
    float y = uv.y;
    float ay = y * canvas_size.y;

    // ── Background + band tints ──────────────────────────────────────────
    vec3 bg = vec3(0.051, 0.051, 0.090);   // #0d0d17
    if (x >= kick_range.x && x <= kick_range.y) bg += vec3(0.10, 0.06, 0.02);
    if (x >= hh_range.x   && x <= hh_range.y)   bg += vec3(0.02, 0.07, 0.10);

    // ── Subtle horizontal dB grid (7 bands) ──────────────────────────────
    float seg = canvas_size.y / 7.0;
    float gy = mod(ay, seg);
    if (gy < 0.8) bg += vec3(0.03);

    // ── Base color for this column (frequency-mapped) ────────────────────
    vec3 base = texture(color_grad, vec2(x, 0.5)).rgb;

    // ── Curve height at this column ──────────────────────────────────────
    float curve = texture(curve_tex, vec2(x, 0.5)).r;
    float curve_y = curve;          // bar top in [0, 1]
    float bar_top_px = curve_y * canvas_size.y;

    vec3 col = bg;

    // ── Line-only mode: anti-aliased curve + symmetric glow ──────────────
    // No bars — just the outline trace tinted by frequency, with a soft
    // bilateral glow so peaks really pop without the visual noise of bars.
    float dist = abs(ay - bar_top_px);

    // Sharp white-blue line (sub-pixel anti-aliased)
    if (dist < 1.5 && curve_y > 0.001) {
        float a = 1.0 - dist / 1.5;
        col = mix(col, vec3(0.95, 0.97, 1.00), a);
    }

    // Wide soft glow on both sides of the curve — accentuates transients
    if (dist < 24.0 && curve_y > 0.001) {
        float glow = exp(-dist / 6.5);
        col = mix(col, base * 1.6, glow * 0.55);
    }

    // ── Band edge lines (orange = kick, cyan = hi-hat) ───────────────────
    float edge_w = 1.5 / canvas_size.x;
    if (abs(x - kick_range.x) < edge_w || abs(x - kick_range.y) < edge_w)
        col = vec3(1.00, 0.65, 0.00);
    if (abs(x - hh_range.x) < edge_w || abs(x - hh_range.y) < edge_w)
        col = vec3(0.00, 0.75, 1.00);

    frag_color = vec4(clamp(col, 0.0, 1.0), 1.0);
}
"""


def _build_color_gradient(n: int = 256) -> np.ndarray:
    """Pre-compute the freq→color RGB gradient for the shader.

    Keypoints are placed at musically-meaningful frequencies; intermediate
    columns get linear interpolation in RGB space.
    """
    fmin, fmax = 25.0, 20000.0
    log_min, log_max = math.log(fmin), math.log(fmax)
    log_range = log_max - log_min
    keypoints = [
        (30,    (0.86, 0.16, 0.24)),  # deep red — sub
        (80,    (1.00, 0.35, 0.20)),  # red — kick fundamental
        (250,   (1.00, 0.67, 0.31)),  # orange — snare body
        (800,   (0.71, 0.94, 0.47)),  # yellow-green — vocals
        (2500,  (0.39, 0.86, 0.78)),  # green-cyan — presence
        (8000,  (0.31, 0.51, 1.00)),  # blue — hi-hat
        (18000, (0.24, 0.31, 1.00)),  # deep blue — cymbals
    ]
    xs_norm = np.array(
        [(math.log(f) - log_min) / log_range for f, _ in keypoints])
    rgb = np.array([c for _, c in keypoints], dtype=np.float32)
    x_lookup = np.linspace(0.0, 1.0, n, dtype=np.float32)
    out = np.empty((n, 3), dtype=np.float32)
    for i in range(3):
        out[:, i] = np.interp(x_lookup, xs_norm, rgb[:, i])
    return out


class GLSpectrumRenderer:
    """Offscreen-rendered spectrum analyzer using moderngl + a fragment shader.

    Lifecycle:
        renderer = GLSpectrumRenderer()           # one-time GL context + shader
        renderer.resize(cw, ch)                    # when canvas size changes
        pixels = renderer.render(curve, kick, hh)  # each frame
    """

    def __init__(self) -> None:
        if not _MGL_OK:
            raise RuntimeError("moderngl not installed (pip install moderngl)")
        # Standalone context — uses the OS's default GL driver (NVIDIA WGL on
        # Windows when an NVIDIA card is present).  No window needed.
        self.ctx = moderngl.create_standalone_context(require=330)
        log.info("GLSpectrumRenderer: %s (%s)",
                 self.ctx.info.get("GL_RENDERER", "?"),
                 self.ctx.info.get("GL_VERSION", "?"))

        self.width  = 0
        self.height = 0
        self.fbo: Any = None
        self.color_tex: Any = None

        self.prog = self.ctx.program(vertex_shader=_VERT, fragment_shader=_FRAG)
        self.prog["curve_tex"]  = 0
        self.prog["color_grad"] = 1

        # Full-screen quad — UVs are flipped vertically so the readback comes
        # out with row 0 = top of the image (matches tkinter / PIL convention).
        quad = np.array([
            -1.0, -1.0, 0.0, 1.0,
             1.0, -1.0, 1.0, 1.0,
            -1.0,  1.0, 0.0, 0.0,
             1.0,  1.0, 1.0, 0.0,
        ], dtype=np.float32)
        self.vbo = self.ctx.buffer(quad.tobytes())
        self.vao = self.ctx.vertex_array(self.prog, [
            (self.vbo, "2f 2f", "in_pos", "in_uv"),
        ])

        # Frequency→color lookup texture (static)
        grad = _build_color_gradient(256)
        self.color_grad_tex = self.ctx.texture((256, 1), 3, dtype="f4",
                                                data=grad.tobytes())
        self.color_grad_tex.repeat_x = False
        self.color_grad_tex.repeat_y = False
        self.color_grad_tex.filter = (moderngl.LINEAR, moderngl.LINEAR)

        # Curve texture is (re)created when the canvas width changes
        self.curve_tex: Any = None
        self._curve_w = 0
        self._curve_buf = bytes(0)   # cached upload buffer

    def resize(self, width: int, height: int) -> None:
        if width == self.width and height == self.height and self.fbo is not None:
            return
        if self.fbo is not None:
            self.fbo.release()
            self.color_tex.release()
        self.width  = int(width)
        self.height = int(height)
        # RGB framebuffer; 8-bit per channel matches the PIL "RGB" mode
        self.color_tex = self.ctx.texture((self.width, self.height), 3)
        self.fbo = self.ctx.framebuffer(color_attachments=[self.color_tex])

    def _ensure_curve_tex(self, width: int) -> None:
        if self._curve_w == width and self.curve_tex is not None:
            return
        if self.curve_tex is not None:
            self.curve_tex.release()
        self.curve_tex = self.ctx.texture((width, 1), 1, dtype="f4")
        self.curve_tex.filter = (moderngl.NEAREST, moderngl.NEAREST)
        self.curve_tex.repeat_x = False
        self.curve_tex.repeat_y = False
        self._curve_w = width

    def render(self,
               curve_heights: np.ndarray,
               kick_range: Tuple[float, float],
               hh_range:   Tuple[float, float]) -> Optional[np.ndarray]:
        """Render one frame and return the pixel bytes as (H, W, 3) uint8.

        ``curve_heights`` must be length ``self.width`` with values in [0, 1].
        ``kick_range`` and ``hh_range`` are normalised x positions in [0, 1].
        """
        if self.fbo is None or self.width == 0 or self.height == 0:
            return None
        if curve_heights.shape[0] != self.width:
            return None

        self._ensure_curve_tex(self.width)
        curve_bytes = curve_heights.astype(np.float32, copy=False).tobytes()
        self.curve_tex.write(curve_bytes)

        self.curve_tex.use(location=0)
        self.color_grad_tex.use(location=1)

        self.prog["canvas_size"] = (float(self.width), float(self.height))
        self.prog["kick_range"]  = (float(kick_range[0]), float(kick_range[1]))
        self.prog["hh_range"]    = (float(hh_range[0]),   float(hh_range[1]))

        self.fbo.use()
        self.ctx.viewport = (0, 0, self.width, self.height)
        self.vao.render(moderngl.TRIANGLE_STRIP)

        # Read the FBO contents back into CPU memory. components=3 → RGB.
        data = self.fbo.read(components=3, alignment=1)
        return np.frombuffer(data, dtype=np.uint8).reshape(
            self.height, self.width, 3)

    def release(self) -> None:
        for r in (self.fbo, self.color_tex, self.curve_tex,
                   self.color_grad_tex, self.vao, self.vbo, self.prog):
            try:
                if r is not None:
                    r.release()
            except Exception:
                pass
        try:
            self.ctx.release()
        except Exception:
            pass


def try_create_renderer() -> Optional[GLSpectrumRenderer]:
    """Returns a working GL renderer, or None if moderngl isn't available
    or the GL context fails to come up."""
    if not _MGL_OK:
        log.info("moderngl not installed; falling back to CPU bitmap renderer")
        return None
    try:
        return GLSpectrumRenderer()
    except Exception as exc:
        log.warning("GL renderer unavailable (%s); using CPU fallback",
                    exc, exc_info=False)
        return None
