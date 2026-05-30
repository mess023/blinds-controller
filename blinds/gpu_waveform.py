"""
GPU-rendered waveform display using moderngl.

Mirrors gpu_spectrum.py: render a fragment shader pass into an offscreen
framebuffer, read back as a numpy array, push through PIL/ImageTk.

The waveform shows peak amplitude over the last 16 beats centred on the
horizontal midline.  Visualisation is plain blue (no Rekordbox-style RGB
colour split — the user asked for it removed).

Requires:
    pip install moderngl
"""

from __future__ import annotations

import logging
from typing import Any, Optional

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

# Fragment shader:
#   amp_tex     — 1-D R32F texture, amplitude per column (0..1)
#   marker_tex  — 1-D R32F texture, 1.0 at beat tick columns
#   canvas_size — pixel dimensions
_FRAG = """
#version 330

in vec2 uv;
out vec4 frag_color;

uniform sampler2D amp_tex;
uniform sampler2D marker_tex;
uniform vec2 canvas_size;

void main() {
    float x = uv.x;
    float y = uv.y;                 // 0 bottom → 1 top

    // ── Alternating dark beat bands (16 across canvas) ───────────────────
    float band = floor(x * 16.0);
    bool even  = mod(band, 2.0) < 0.5;
    vec3 bg = even ? vec3(0.059, 0.059, 0.122)
                   : vec3(0.071, 0.071, 0.122);

    // ── Beat tick lines: darker vertical bars at detected beat positions ─
    float marker = texture(marker_tex, vec2(x, 0.5)).r;
    if (marker > 0.5) bg = vec3(0.165, 0.165, 0.290);

    // ── Look up bar amplitude at this column ─────────────────────────────
    float amp     = texture(amp_tex, vec2(x, 0.5)).r;
    float half_h  = amp * 0.46;     // 92 % of canvas centred on midline
    float mid     = 0.5;
    float dy      = abs(y - mid);

    vec3 col = bg;
    if (dy <= half_h) {
        // Brighter near the edges of the bar (slight outline emphasis)
        float t = dy / max(half_h, 0.001);
        col = vec3(0.54, 0.71, 0.98) * (0.65 + 0.35 * t);
    }

    // Right-edge "now" marker (2 px highlighted)
    if (x > 1.0 - 2.0 / canvas_size.x) col = vec3(0.98, 0.89, 0.69);

    frag_color = vec4(col, 1.0);
}
"""


class GLWaveformRenderer:
    """Offscreen GL renderer for the waveform display."""

    def __init__(self) -> None:
        if not _MGL_OK:
            raise RuntimeError("moderngl not installed (pip install moderngl)")
        self.ctx = moderngl.create_standalone_context(require=330)
        log.info("GLWaveformRenderer: %s (%s)",
                 self.ctx.info.get("GL_RENDERER", "?"),
                 self.ctx.info.get("GL_VERSION", "?"))

        self.width  = 0
        self.height = 0
        self.fbo: Any = None
        self.color_tex: Any = None

        self.prog = self.ctx.program(vertex_shader=_VERT, fragment_shader=_FRAG)
        self.prog["amp_tex"]    = 0
        self.prog["marker_tex"] = 1

        # Y-flipped UVs so the readback is row-0 = top (PIL/Tk convention)
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

        self.amp_tex: Any = None
        self.marker_tex: Any = None
        self._tex_w = 0

    def resize(self, width: int, height: int) -> None:
        if width == self.width and height == self.height and self.fbo is not None:
            return
        if self.fbo is not None:
            self.fbo.release()
            self.color_tex.release()
        self.width  = int(width)
        self.height = int(height)
        self.color_tex = self.ctx.texture((self.width, self.height), 3)
        self.fbo = self.ctx.framebuffer(color_attachments=[self.color_tex])

    def _ensure_textures(self, width: int) -> None:
        if self._tex_w == width and self.amp_tex is not None:
            return
        for t in (self.amp_tex, self.marker_tex):
            if t is not None:
                t.release()
        self.amp_tex = self.ctx.texture((width, 1), 1, dtype="f4")
        self.amp_tex.filter = (moderngl.NEAREST, moderngl.NEAREST)
        self.amp_tex.repeat_x = False
        self.amp_tex.repeat_y = False
        self.marker_tex = self.ctx.texture((width, 1), 1, dtype="f4")
        self.marker_tex.filter = (moderngl.NEAREST, moderngl.NEAREST)
        self.marker_tex.repeat_x = False
        self.marker_tex.repeat_y = False
        self._tex_w = width

    def render(self,
               amplitudes: np.ndarray,
               markers: np.ndarray) -> Optional[np.ndarray]:
        """``amplitudes`` and ``markers`` are length-W float32 arrays.
        Returns (H, W, 3) uint8."""
        if self.fbo is None or self.width == 0 or self.height == 0:
            return None
        if amplitudes.shape[0] != self.width:
            return None

        self._ensure_textures(self.width)
        self.amp_tex.write(amplitudes.astype(np.float32, copy=False).tobytes())
        self.marker_tex.write(markers.astype(np.float32, copy=False).tobytes())
        self.amp_tex.use(location=0)
        self.marker_tex.use(location=1)

        self.prog["canvas_size"] = (float(self.width), float(self.height))

        self.fbo.use()
        self.ctx.viewport = (0, 0, self.width, self.height)
        self.vao.render(moderngl.TRIANGLE_STRIP)

        data = self.fbo.read(components=3, alignment=1)
        return np.frombuffer(data, dtype=np.uint8).reshape(
            self.height, self.width, 3)

    def release(self) -> None:
        for r in (self.fbo, self.color_tex, self.amp_tex,
                   self.marker_tex, self.vao, self.vbo, self.prog):
            try:
                if r is not None:
                    r.release()
            except Exception:
                pass
        try:
            self.ctx.release()
        except Exception:
            pass


def try_create_renderer() -> Optional[GLWaveformRenderer]:
    if not _MGL_OK:
        return None
    try:
        return GLWaveformRenderer()
    except Exception as exc:
        log.warning("GL waveform renderer unavailable (%s); CPU fallback",
                    exc, exc_info=False)
        return None
