# APC40 MK2 LED Color Palette

**Total Colors:** 128 RGB values  
**Format:** Hexadecimal RGB (0xRRGGBB)

## Color Palette (Index 0-127)

```
Index  Color      RGB
  0    0x000000   Black
  1    0x1E1E1E   Very Dark Gray
  2    0x7F7F7F   Medium Gray
  3    0xFFFFFF   White
  4    0xFF4C4C   Light Red
  5    0xFF0000   Red
  6    0x590000   Dark Red
  7    0x190000   Very Dark Red
  8    0xFFBD6C   Light Orange
  9    0xFF5400   Orange
 10    0x591D00   Dark Orange
 11    0x271B00   Very Dark Orange
 12    0xFFFF4C   Light Yellow
 13    0xFFFF00   Yellow
 14    0x595900   Dark Yellow
 15    0x191900   Very Dark Yellow
 16    0x88FF4C   Light Yellow-Green
 17    0x54FF00   Yellow-Green
 18    0x1D5900   Dark Yellow-Green
 19    0x142B00   Very Dark Yellow-Green
 20    0x4CFF4C   Light Green
 21    0x00FF00   Pure Green
 22    0x005900   Dark Green
 23    0x001900   Very Dark Green
 24    0x4CFF5E   Light Spring Green
 25    0x00FF19   Spring Green
 26    0x00590D   Dark Spring Green
 27    0x001902   Very Dark Spring Green
 28    0x4CFF88   Light Sea Green
 29    0x00FF55   Sea Green
 30    0x00591D   Dark Sea Green
 31    0x001F12   Very Dark Sea Green
 32    0x4CFFB7   Light Turquoise
 33    0x00FF99   Turquoise
 34    0x005935   Dark Turquoise
 35    0x001912   Very Dark Turquoise
 36    0x4CC3FF   Light Cyan
 37    0x00A9FF   Cyan
 38    0x004152   Dark Cyan
 39    0x001019   Very Dark Cyan
 40    0x4C88FF   Light Sky Blue
 41    0x0055FF   Sky Blue
 42    0x001D59   Dark Sky Blue
 43    0x000819   Very Dark Sky Blue
 44    0x4C4CFF   Light Blue
 45    0x0000FF   Pure Blue
 46    0x000059   Dark Blue
 47    0x000019   Very Dark Blue
 48    0x874CFF   Light Violet
 49    0x5400FF   Violet
 50    0x190064   Dark Violet
 51    0x0F0030   Very Dark Violet
 52    0xFF4CFF   Light Magenta
 53    0xFF00FF   Magenta
 54    0x590059   Dark Magenta
 55    0x190019   Very Dark Magenta
 56    0xFF4C87   Light Pink
 57    0xFF0054   Hot Pink
 58    0x59001D   Dark Pink
 59    0x220013   Very Dark Pink
 60    0xFF1500   Red-Orange
 61    0x993500   Dark Red-Orange
 62    0x795100   Brown
 63    0x436400   Olive Green
 64    0x033900   Forest Green
 65    0x005735   Teal
 66    0x00547F   Steel Blue
 67    0x0000FF   Bright Blue
 68    0x00454F   Dark Slate Blue
 69    0x2500CC   Deep Blue
 70    0x7F7F7F   Gray
 71    0x202020   Dark Gray
 72    0xFF0000   Red
 73    0xBDFF2D   Lime Green
 74    0xAFED06   Bright Lime
 75    0x64FF09   Neon Green
 76    0x108B00   Dark Lime
 77    0x00FF87   Mint Green
 78    0x00A9FF   Sky Blue
 79    0x002AFF   Electric Blue
 80    0x3F00FF   Violet Blue
 81    0x7A00FF   Purple
 82    0xB21A7D   Magenta Pink
 83    0x402100   Dark Brown
 84    0xFF4A00   Bright Orange
 85    0x88E106   Lime
 86    0x72FF15   Neon Lime
 87    0x00FF00   Green
 88    0x3BFF26   Light Green
 89    0x59FF71   Pale Green
 90    0x38FFCC   Light Cyan
 91    0x5B8AFF   Periwinkle
 92    0x3151C6   Medium Blue
 93    0x877FE9   Light Purple
 94    0xD31DFF   Fuchsia
 95    0xFF005D   Red Pink
 96    0xFF7F00   Orange
 97    0xB9B000   Olive Yellow
 98    0x90FF00   Chartreuse
 99    0x835D07   Dark Brown
100    0x392b00   Very Dark Brown
101    0x144C10   Dark Forest Green
102    0x0D5038   Deep Teal
103    0x15152A   Dark Navy
104    0x16205A   Navy Blue
105    0x693C1C   Leather Brown
106    0xA8000A   Dark Red
107    0xDE513D   Salmon
108    0xD86A1C   Burnt Orange
109    0xFFE126   Golden Yellow
110    0x9EE12F   Yellow Green
111    0x67B50F   Olive
112    0x1E1E30   Charcoal
113    0xDCFF6B   Pale Lime
114    0x80FFBD   Aquamint
115    0x9A99FF   Lavender
116    0x8E66FF   Soft Purple
117    0x404040   Dark Gray
118    0x757575   Medium Gray
119    0xE0FFFF   Cyan White
120    0xA00000   Maroon
121    0x350000   Dark Maroon
122    0x1AD000   Bright Green
123    0x074200   Deep Green
124    0xB9B000   Mustard Yellow
125    0x3F3100   Dark Gold
126    0xB35F00   Rust Orange
127    0x4B1502   Deep Red-Brown
```

## Python Reference

For use in your blinds-controller code:

```python
APC_LED_PALETTE = [
    0x000000, 0x1E1E1E, 0x7F7F7F, 0xFFFFFF, 0xFF4C4C, 0xFF0000, 0x590000, 0x190000,
    0xFFBD6C, 0xFF5400, 0x591D00, 0x271B00, 0xFFFF4C, 0xFFFF00, 0x595900, 0x191900,
    0x88FF4C, 0x54FF00, 0x1D5900, 0x142B00, 0x4CFF4C, 0x00FF00, 0x005900, 0x001900,
    0x4CFF5E, 0x00FF19, 0x00590D, 0x001902, 0x4CFF88, 0x00FF55, 0x00591D, 0x001F12,
    0x4CFFB7, 0x00FF99, 0x005935, 0x001912, 0x4CC3FF, 0x00A9FF, 0x004152, 0x001019,
    0x4C88FF, 0x0055FF, 0x001D59, 0x000819, 0x4C4CFF, 0x0000FF, 0x000059, 0x000019,
    0x874CFF, 0x5400FF, 0x190064, 0x0F0030, 0xFF4CFF, 0xFF00FF, 0x590059, 0x190019,
    0xFF4C87, 0xFF0054, 0x59001D, 0x220013, 0xFF1500, 0x993500, 0x795100, 0x436400,
    0x033900, 0x005735, 0x00547F, 0x0000FF, 0x00454F, 0x2500CC, 0x7F7F7F, 0x202020,
    0xFF0000, 0xBDFF2D, 0xAFED06, 0x64FF09, 0x108B00, 0x00FF87, 0x00A9FF, 0x002AFF,
    0x3F00FF, 0x7A00FF, 0xB21A7D, 0x402100, 0xFF4A00, 0x88E106, 0x72FF15, 0x00FF00,
    0x3BFF26, 0x59FF71, 0x38FFCC, 0x5B8AFF, 0x3151C6, 0x877FE9, 0xD31DFF, 0xFF005D,
    0xFF7F00, 0xB9B000, 0x90FF00, 0x835D07, 0x392b00, 0x144C10, 0x0D5038, 0x15152A,
    0x16205A, 0x693C1C, 0xA8000A, 0xDE513D, 0xD86A1C, 0xFFE126, 0x9EE12F, 0x67B50F,
    0x1E1E30, 0xDCFF6B, 0x80FFBD, 0x9A99FF, 0x8E66FF, 0x404040, 0x757575, 0xE0FFFF,
    0xA00000, 0x350000, 0x1AD000, 0x074200, 0xB9B000, 0x3F3100, 0xB35F00, 0x4B1502,
]
```

## Usage

Access color by index:
```python
red = APC_LED_PALETTE[5]      # 0xFF0000
green = APC_LED_PALETTE[21]   # 0x00FF00
blue = APC_LED_PALETTE[45]    # 0x0000FF
```

Or for velocity-style LED control (0-127 maps to index 0-127):
```python
self._midi_out.send(mido.Message("note_on", note=some_note, velocity=color_index, channel=0))
```
