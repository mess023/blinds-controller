# Blinds Controller Project — Development History

**Last Updated:** 2026-05-29 (Session 2 continuation)  
**Repository:** [blinds-controller](https://github.com/mess023/blinds-controller)

---

## Overview

This document chronicles the development of MIDI/Art-Net control, APC40 MK2 integration, audio BPM detection, and GUI redesign for the motorised window blinds controller. The work spans code refactoring, hardware integration, LED feedback, and comprehensive documentation.

---

## Timeline & Milestones

### 🎯 **Milestone 1: MIDI Input & APC40 Clip-Grid Mapping** (Session Start)

**Objective:** Add MIDI control via AKAI APC40 MK2 hardware controller.

**What Was Done:**
- Researched APC40 MK2 protocol (Generic Mode 0, official Communications Protocol v1.2)
- Discovered clip-pad grid encoding: `note = (4-row)*8 + column` (notes 0–39)
- Master section buttons on CC notes: BPM sync (87), audio (88), Link (89), resync (90), tap (99), nudge −/+ (100/101)
- Continuous controls: Gap Position (CC 7 on fader 1), Gap Size (CC 48 on knob 1)
- Implemented MIDI input handler in `blinds_controller.py`
- Added mido/python-rtmidi imports with optional-dependency guards
- Extended BEAT_OPTIONS with ("128", 128.0) entry

**Key Files Created/Modified:**
- `blinds_controller.py`: Added MIDI message dispatcher
- `requirements.txt`: Added mido, python-rtmidi, Pillow

**Problems Encountered:**
- ✗ **Type checking on optional imports**: Pyright flagged "mido is not a known attribute of None" (~47 errors). Fixed by typing all optional modules as `Any` with `MIDI_AVAILABLE` guards. → **Result: 0 errors, 0 warnings**

---

### 🎯 **Milestone 2: APC40 Canvas Overlay & GUI Redesign**

**Objective:** Display APC40 image as background in tkinter canvas with transparent text overlays for all controls.

**What Was Done:**
- Added `apc40_mk2-EMPTY.png` (1100×733) as canvas background
- Created `_build_apc40_canvas()` method with Pillow image loading + LANCZOS scaling
- Pixel-sampled exact button/knob centres from the image:
  - Yellow clip pads: RGB >200,>170,<130 → found 32 positions (4 rows × 8 cols)
  - Cream master buttons: RGB >220,>215,180-230 → found 7 positions
  - Black knobs: RGB <15,<15,<15 → found device knobs + Gap Size knob
- Implemented transparent canvas overlays: text labels + invisible hit rectangles (no widget backgrounds)
- Created `_mk_canvas_btn()` for clip pads and master buttons
- Added Gap Position fader with yellow marker line + drag control
- Added Gap Size knob with drag (100px travel = 0–25% range) + scroll wheel (±0.5%/0.1% per notch)
- Added BPM display at canvas position (927, 50)

**Key Files Created/Modified:**
- `blinds_controller.py`: Added Pillow import, `_build_apc40_canvas()` method, fader/knob overlays

**Problems Encountered:**
- ✗ **Widget buttons blocked the image**: Early implementation used tk.Button as canvas children with opaque backgrounds. Rewrote as invisible rectangles + text overlays. → **Result: Clean transparent layout**
- ✗ **Button text misaligned 20–120px**: Pixel colour sampling measured exact centres (yellow, cream, black bands) rather than guessing. → **Generated verification image with crosshairs confirming all 32 positions**
- ✗ **Pillow LANCZOS constant location varies**: Newer Pillow versions moved LANCZOS to `Image.Resampling.LANCZOS`. Used `getattr()` for backward compatibility.

---

### 🎯 **Milestone 3: LED Feedback & Beat Chase**

**Objective:** Bidirectional MIDI: controller sends state changes to app, app sends LED feedback back to controller.

**What Was Done:**
- Implemented `_refresh_apc_leds()` called from all state-change handlers
- Clip grid LEDs: bright green (velocity 21) when lit; off otherwise
- Master buttons: toggle state (on/off); BPM sync, audio sync, Link toggled independently
- Gap Size knob LED ring: set to Volume style (CC 0x38=56, value 2); fills 0–127 as value changes
- Beat visualiser on Device Control knob rings (CC 0x10–0x17, channel 0):
  - 8-beat cycle: each beat advances to next knob
  - Within each beat: single LED scans clockwise around 15-position ring (1 LED per 1/15th beat)
  - Single ring style (CC 0x18–0x1F, value 1) for clean scanning effect; Volume style (value 2) used for Gap Size only
- Track-select row beat chase (attempted): tried 8 buttons for left→right scanning with orange trail

**Key Files Created/Modified:**
- `blinds_controller.py`: Added `_led_set()`, `_led_btn()`, `_led_ring()`, `_refresh_apc_leds()`, `_apc_leds_off()`, `_chase_leds_tick()`

**Problems Encountered:**
- ✗ **Pattern "Still" button always on**: Radio-group logic was: "Still = off if sync_beats is None else pattern name matches". Fixed to: "Still lights only when sync_beats is None; other patterns light when sync_beats is a beat value AND name matches" → **Mutually exclusive now**
- ✗ **Both "Still" and "Uniform" lit at startup**: `_param_block` default-setting called `_set_size_pattern("Uniform")`, which marked it as selected. Reset `_size_pat_selected = "Still"` after `_build_ui()` to undo this. → **Only Still lit at startup now**
- ✗ **Beat chase LED trail "messy"**: Track-select buttons (note 0x33) are single-colour on/off only per protocol; velocity has no effect. Attempted visual fading failed. → **Removed the track-select chase; kept clean beat-ring visualiser instead**

---

### 🎯 **Milestone 4: Motor Speed Fader & Nudge Button Fixes**

**Objective:** Add motor speed control on master fader; fix NUDGE+/- button positions.

**What Was Done:**
- Moved motor speed from channel 8 fader (CC 7) to **master fader** (CC 14, channel 0)
- Updated position in `APC40_POS`: `"fader_master": (722, 520, 57, 188)`
- Range: 5–100 %/s (instead of channel 8's 0–100%)
- Added motor fader marker line + drag control, yellow centre line updates on value change
- Fixed NUDGE button positions: swapped to match image labels
  - NUDGE + on left button (x=886)
  - NUDGE − on right button (x=967)

**Key Files Created/Modified:**
- `blinds_controller.py`: Rewrote fader sections; fixed master fader constant
- `blinds/constants.py`: Updated `APC_CC_MOTOR_SPD_CH` and `APC_CC_MOTOR_SPD`

**Problems Encountered:**
- ✗ **Fader "knob" graphics looked messy**: Attempted to create animated moving caps by erasing static knobs from PIL image + drawing canvas graphics on top. Visual result looked cluttered. → **Reverted to clean yellow marker lines (original approach)**
- ✗ **Motor speed MIDI reception wrong**: Initially CC 7 on channel 7 (track 8). Updated to CC 14 channel 0 (master fader).
- ✗ **Gap Size LED round-trip asymmetry**: Old code: `int(gap_size / 25 * 127)` truncated downward → every CW detent felt stuck, CCW felt 2x fast. Fixed: `int(round(...))` → **Symmetric now, 0 mismatches across all 128 values**

---

### 🎯 **Milestone 5: Code Refactoring into Modular Package**

**Objective:** Split 2448-line monolithic file into organized `blinds/` package.

**What Was Done:**
- Created package structure:
  - `blinds/__init__.py` (empty marker)
  - `blinds/constants.py` (174 lines) — all module config, APC40_POS, colours, `_wave()`, `_apc_clip_note()`
  - `blinds/network.py` (100 lines) — Art-Net + OSC (sockets, send_universe, osc_send)
  - `blinds/beat.py` (43 lines) — BeatClock class
  - `blinds/audio.py` (374 lines) — AudioBPMDetector, get_audio_devices, optional sounddevice/numpy/aubio/pyaudiowpatch imports
  - `blinds/link.py` (76 lines) — Ableton Link optional imports + helpers
  - `blinds/midi.py` (16 lines) — mido optional import + MIDI_AVAILABLE flag
  - `blinds/ui_utils.py` (18 lines) — `_hr()`, `_hov()`, `ttk`, `Any` re-exports
  - `blinds/app.py` (1698 lines) — BlindsApp class (all methods unchanged)
- Updated entry point: `blinds_controller.py` (30 lines) now just imports BlindsApp and calls mainloop()
- All optional imports moved to their respective modules; app.py imports resolved names
- No circular imports; pyright clean (0 errors)

**Key Files Created/Modified:**
- Created 8 new files in `blinds/` package
- Replaced old monolithic `blinds_controller.py` with thin entry point

**Problems Encountered:**
- None — refactoring was clean, no functional changes

---

### 🎯 **Milestone 6: Connection Guide for Lighting Technicians**

**Objective:** Create professional reference for Art-Net and OSC control of the frames.

**What Was Done:**
- Created `CONNECTION_GUIDE.html` (dark-themed, professional)
- Sections:
  - Quick-reference badges: subnet, ports, channels, hardware
  - Network setup: IP table, topology diagram, connectivity test
  - Art-Net: universe, DMX channel map (all 4 frames × 4 channels), common 16-bit values, console patching examples (MagicQ, Resolume, TouchDesigner, QLC+)
  - OSC: position endpoints, calibration endpoints, status reply format with 8 arguments, TouchDesigner + QLab examples
  - First-time calibration: step-by-step, web UI browser access
  - Troubleshooting: 6 common issues with root causes and fixes
- Print-optimised CSS for PDF export

**Key Files Created/Modified:**
- `blinds-controller/CONNECTION_GUIDE.html` (1500+ lines of HTML/CSS)

**Problems Encountered:**
- None — documentation task, no code involved

---

### 🎯 **Milestone 7: WT32-ETH01 GPIO Pinout Reference**

**Objective:** Create visual reference mapping all GPIO pins to their project functions.

**What Was Done:**
- Initial attempt: `WT32-ETH01_PINOUT_REFERENCE.html` with detailed category-based table
- Second attempt: **Annotated the original `WT32-ETH01_pinout_LL.png` image** directly with PIL:
  - Extracted all 10 GPIO pins from ESP32 code:
    - GPIO 2, 4, 17, 12: limit switches (bottom/top start/end)
    - GPIO 5: motor driver alarm
    - GPIO 13: shared stepper enable
    - GPIO 14, 15: top stepper (DIR, STEP)
    - GPIO 32, 33: bottom stepper (STEP, DIR)
  - Added text annotations overlaid on the image showing each pin's function
  - Colour-coded for easy identification
- Moved final `WT32-ETH01_pinout_ANNOTATED.png` to ESP32blinds project

**Key Files Created/Modified:**
- `ESP32blinds/WT32-ETH01_pinout_ANNOTATED.png` (image with overlaid text)

**Problems Encountered:**
- ✗ **PIL not installed initially**: Installed Pillow, then worked
- ✗ **Unicode encoding error**: Checkmark character (✓) caused Windows console encoding issue. Removed special chars, used ASCII only. → **Success**

---

## Code Quality Metrics

| Metric | Session 1 | Session 2 | Notes |
|--------|----------|----------|-------|
| Monolithic file | 2448 lines | 30-line entry point + 8 modules | Refactored to package structure |
| Pyright errors | 47 (optional imports) | 0 | Fixed with `Any` guards |
| Type coverage | Partial | Full with `Any` guards | All optional modules properly typed |
| Canvas width | 1100px | 1222px | Corrected to match image aspect ratio |
| GUI columns | 1 (vertical stack) | 3 (left/center/right) | Better screen utilization |
| MIDI controls | 3 (Gap Pos, Gap Size, Motor) | 4 (+ BPM fine adjust) | Precise tempo control added |
| Documentation | README + HTML guide | + COLOR reference + PROJECT history | Comprehensive coverage |
| Hardware verified | None | 5+ controls tested | BPM knob, buttons, faders all working |

---

## Problems & Errors Summary

### Critical Issues (Resolved)

1. **Type checking on optional imports** → Fixed with `Any` typing + guards
2. **Widget buttons blocking image** → Rewrote as invisible rectangles
3. **Button text misalignment (20–120px)** → Pixel sampling measured exact centres
4. **"Still" button always lit** → Fixed radio-group logic to be mutually exclusive
5. **Gap Size LED asymmetry (CW slow, CCW fast)** → Fixed truncation bug with `round()`
6. **Motor speed on wrong fader** → Moved from ch8 (CC 7) to master (CC 14)
7. **Fader graphics looked messy** → Reverted to clean yellow marker lines

### UI/UX Refinements

1. **Label colour mismatch** → Changed from pink (#f38ba8) to pure red (#ff0000)
2. **Label alignment and size** → Set to match printed APC40 labels (8pt normal, -16px offset)
3. **NUDGE button positions** → Swapped to match image (left = NUDGE+, right = NUDGE−)

### Architectural Decisions

1. **Canvas text overlays vs widget buttons** → Transparent overlays (no background blocks)
2. **Beat visualiser location** → Device Control knob rings (8 knobs, 8-beat cycle)
3. **Master fader for motor speed** → Makes sense contextually, not a random channel

---

### 🎯 **Milestone 8: Beat Chase Buttons & Device Control Button Correction**

**Objective:** Add visual beat chasing to device control buttons below the knobs; correct MIDI note mappings.

**What Was Done:**
- Identified correct MIDI notes from MIDI mapping screenshot (0x3A–0x41): A#2-C#3 (top row), D3-F3 (bottom row)
- Converted note names to MIDI numbers: [58-65] decimal = [0x3A-0x41] hex
- Added `APC_NOTE_DEVICE_BTN_ALL` constant to constants.py
- Implemented beat chase visualization on 8 buttons:
  - 8-beat cycle synchronized with knob ring visualizer
  - Buttons flash on/off (on for first half of beat, off for second half)
  - Each button corresponds to one beat in sequence
- Buttons light with velocity control (note_on/note_off messages)

**Key Files Modified:**
- `blinds/constants.py`: Added device button note constants
- `blinds/app.py`: Enhanced `_chase_leds_tick()` for button LED updates, `_apc_leds_off()` for cleanup

**Problems Encountered:**
- ✗ **Velocity dimming doesn't work on buttons**: Initial pulse effect (varying velocity 20→120) failed. Fixed by switching to on/off flash pattern. → **Clean visual indicator achieved**
- ✗ **MIDI note lookup confusion**: User's screenshot showed note names, not numbers. Converted properly: A#2=58, C#3=61, etc. → **Correct mapping verified**

---

### 🎯 **Milestone 9: Canvas Aspect Ratio Correction & Control Labels**

**Objective:** Match canvas to original image aspect ratio; add labels for all controls.

**What Was Done:**
- Recalculated canvas dimensions: original image 4968×2982 (ratio 1.666:1), current 1100×733 (ratio 1.501:1)
- Resized canvas from 1100×733 to **1222×733** to match aspect ratio
- Scaled all APC40_POS coordinates by factor 1.222/1.100 = 1.111 for X-axis
- Added control labels on canvas:
  - "GAP POS" label above Gap Position fader (white text, 7pt bold)
  - "GAP SIZE" label above Gap Size knob (white text, 7pt bold)
  - Motor speed percentage display above master fader (e.g., "75%/s", yellow text, 8pt bold)
- Motor speed percentage updates live via trace callback

**Key Files Modified:**
- `blinds/constants.py`: Updated APC40_W, APC40_H, all APC40_POS coordinates
- `blinds/app.py`: Added label text items, motor speed display with trace update

**Problems Encountered:**
- ✗ **Label alignment**: Initial positions were off. Fine-tuned y-offsets to position labels symmetrically above controls. → **Professional appearance achieved**

---

### 🎯 **Milestone 10: 3-Column GUI Layout Redesign**

**Objective:** Reorganize GUI layout to use screen space efficiently; hide duplicate controls already on canvas.

**What Was Done:**
- Redesigned main layout from vertical stack to 3-column grid:
  - **Left column**: Frame cards (vertical stack, 220px wide)
  - **Center column**: APC40 canvas (1222×733)
  - **Right column**: Art-Net controls + Audio device selection (400-500px)
- Removed duplicate controls from right column:
  - Deleted `_build_gap_section()` (already on canvas)
  - Deleted `_build_bpm_section()` (already on canvas)
  - Kept only Art-Net controls and audio device selector
- Updated window minsize to 1900×800 to accommodate new layout
- Frame cards refactored: created `_build_frame_cards_vertical()` for left column stacking
- All build methods updated to accept optional `parent` parameter for flexible layout

**Key Files Modified:**
- `blinds/app.py`: Major refactor of `_build_ui()`, added parent parameter support to layout methods

**Problems Encountered:**
- ✗ **Widget initialization order**: `_poll_link()` called before UI built, causing AttributeError on `_link_lbl`. Moved to `self.after(0, ...)` for deferred execution. → **Proper initialization sequence restored**
- ✗ **Frame card parameter naming**: After renaming `col` to `idx`, leftover references caused NameError. Systematically renamed all references. → **Clean refactoring completed**

---

### 🎯 **Milestone 11: BPM Fine Adjust Knob Implementation**

**Objective:** Add precise tempo adjustment via encoder knob; map to CC 13.

**What Was Done:**
- Added `APC_CC_BPM_FINE = 13` constant
- Added knob position to APC40_POS: (1160, 178)
- Created canvas overlay with diamond indicator + two-line label ("BPM\nfine adjust")
- Implemented relative encoder handler:
  - CC value 64 = center/no change
  - CC < 64 = CW turn (right) → +0.01 BPM per step
  - CC > 64 = CCW turn (left) → -0.01 BPM per step
- Label styled to match hardware button text (8pt white, bold)

**Key Files Modified:**
- `blinds/constants.py`: Added APC_CC_BPM_FINE, knob_bpm_fine position
- `blinds/app.py`: Created knob overlay, implemented CC 13 handler, integrated into `_handle_midi()`

**Problems Encountered:**
- ✗ **Relative encoder decoding (attempt 1)**: Tracked delta between consecutive CC values → accumulated incorrectly. → **Switched to direct value decoding**
- ✗ **Magnitude scaling issue (attempt 2)**: Multiplied full delta by 0.01 → added 0.63 BPM per click (63 units × 0.01). Fixed by treating each message as single ±0.01 step. → **Proper incremental adjustment achieved**
- ✗ **Direction inverted (attempt 3)**: Initial logic reversed CW/CCW. Fixed mapping: < 64 = CW (positive), > 64 = CCW (negative). → **Correct physical rotation behavior**
- ✗ **Visual label alignment**: Initial single-line label needed improvement. Added two-line format, increased font size to 8pt, adjusted y-position for better spacing. → **Clean professional appearance**

**Result:** Knob now responds intuitively to physical rotation with precise ±0.01 BPM steps.

---

## Unexecuted Ideas & Parked Tasks

### 🔷 **C++/Qt Rewrite (Parked)**

**Status:** Discussed, estimated, NOT STARTED  
**Why:** Python/tkinter GUI feels dated; C++/Qt (QML) would be GPU-accelerated and professional-looking  
**Estimate:** ~15–20 conversation exchanges, ~1 focused week including hardware testing  
**Key Notes:**
- Official Ableton Link C++ SDK available (no binding needed)
- aubio C API for BPM detection (same library as Python)
- WASAPI loopback (Windows) replaces pyaudiowpatch
- Risk: aubio vcpkg compilation uncertain; fallback is energy-based onset detector

**How to Start:** Create CMake scaffold + blank Qt window to validate toolchain before porting logic

### 🔷 **Screen 4 Calibration (Hardware)**

**Status:** NOT DONE  
**Task:** Calibrate reverse1=false setting for Screen 4 (if applicable)  
**Related to:** Earlier-session microstep calibration work

### 🔷 **DM542T Microstep Settings Verification**

**Status:** NOT DONE  
**Task:** Check DM542T SW5–SW8 microstep settings at installation (from earlier session notes)  
**Importance:** Affects stepper resolution and smoothness

### 🔷 **Audio BPM Detection Improvements**

**Status:** BASIC IMPLEMENTATION DONE; OPTIMIZATIONS POSSIBLE  
**Ideas Discussed but Not Pursued:**
- Fine-tuning aubio parameters for different music genres
- Multi-tap BPM averaging over multiple beats
- Octave-fold refinements for edge cases
- Integration with hardware feedback (e.g., LED flashing on beat detection)

### 🔷 **MIDI Learning Mode**

**Status:** IDEA ONLY  
**Concept:** Allow users to map APC40 buttons dynamically (press button on APC40, then bind to app function)  
**Why Not Done:** Out of scope; current hardcoded mapping works well

### 🔷 **Art-Net Broadcast Optimization**

**Status:** WORKING; OPTIMIZATION NOT EXPLORED  
**Idea:** Investigate selective unicast to reduce network traffic if running on large subnets  
**Current:** Broadcast to 10.0.0.255 (all frames get all packets)

### 🔷 **Web UI Polish**

**Status:** FUNCTIONAL; UI NOT REDESIGNED  
**Ideas Mentioned:**
- Dark mode theme (matches app but not implemented on web side)
- Real-time waveform visualization (audio detection)
- Interactive APC40 simulator for testing without hardware

---

## Git Commit History

### blinds-controller repo

| Commit | Message | Key Changes |
|--------|---------|-------------|
| 8fc7cd1 | Add closed-overlap fine-tune + Open-100% park | Motor control refinements |
| 1a25df9 | Calibration UI + telemetry + BPM wrapper | Framework for calibration |
| 931a5df | Initial commit | Founding state |
| **3a423b5** | **Add MIDI/APC40 control + GUI redesign** | **Major milestone: MIDI in/out, canvas overlay, LED feedback** |
| 23e6ad0 | Fix NUDGE+/− button positions | Layout correction |
| 864f6b7 | Move pinout reference to ESP32blinds | File relocation |
| **f01550f** | **Fix encoder knob order + beat chase buttons** | **Milestone 8: Device button visualization + MIDI note correction** |
| 5609f54 | Change button LED effect to on/off flash | Beat chase refinement |
| 35904ab | Add APC40 LED color palette reference | Documentation |
| **d2fb91a** | **Redesign GUI to 3-column layout** | **Milestone 10: Better screen utilization, removed duplication** |
| fb0d1aa | Fix frame_card parameter references | Bug fix |
| **b88ca73** | **Correct canvas aspect ratio + add labels** | **Milestone 9: 1222px width, motor speed display, control labels** |
| 6f60a52 | Fix initialization order for _poll_link | Startup fix |
| **6fbb06c** | **Add BPM fine adjust knob (CC 13)** | **Milestone 11: Tempo fine control with ±0.01 BPM steps** |
| 30fed14 | Fix BPM knob relative encoder handling | Encoder decoding |
| 3966451 | Add label for BPM fine adjust knob | UI refinement |
| 883a41e | Fix BPM knob relative encoder decoding | Final encoder fix |

### ESP32blinds repo

| Commit | Message | Key Changes |
|--------|---------|-------------|
| 035fb85 | (prior state) | Firmware baseline |
| **aba55c8** | **Add WT32-ETH01 pinout reference** | GPIO function documentation |
| b65cbb6 | Replace HTML with annotated image | Visual pinout (PIL-annotated) |

---

## Testing & Validation Status

### Completed ✅

- Type checking: 0 errors, 0 warnings (pyright)
- Smoke tests: App loads, canvas renders, MIDI handler present
- Code structure: 8-module package + thin entry point
- Documentation: HTML guides + annotated image

### Tested on Hardware ❓

- MIDI input/output with real APC40 MK2: **Partially tested**
  - Beat chase buttons: ✅ Working (on/off flash visualization)
  - BPM fine adjust knob: ✅ Working (±0.01 BPM per step, CW/CCW correct)
  - Gap Position fader: ✅ Working
  - Gap Size knob: ✅ Working
  - Motor Speed fader: ✅ Working
- CC number mappings: **Hardware verified**
- LED feedback brightness & colours: **Verified working**
- Beat visualiser on device knobs: ✅ **Verified working**

### Not Tested

- Audio BPM under live DJ conditions (was tested in development)
- All 4 frame synchronization under Art-Net
- Ableton Link sync under stable network conditions

---

## Key Design Decisions Documented

1. **Transparent canvas overlays, not widgets** — Blocks background image otherwise
2. **Beat visualiser on device knob rings (8 knobs × 15 LEDs)** — Clean 8-beat cycle, one knob per beat
3. **Beat chase buttons (8-button flash pattern)** — Synchronized with knob rings; on/off only (no velocity dimming)
4. **Master fader for motor speed** — Contextually sensible for speed control
5. **Pixel sampling for exact button centres** — No guessing; measured RGB bands
6. **`round()` not `int()` for LED feedback** — Ensures CW and CCW detents feel equal
7. **Modular package structure** — Keeps concerns separated; easier to maintain
8. **3-column GUI layout** — Left: frames, Center: APC40, Right: controls; optimized for 1920×1080
9. **Canvas aspect ratio correction (1100→1222px)** — Matches original image 1.666:1 ratio
10. **Relative encoder decoding for BPM knob** — Direct value interpretation (64=center, <64=CW, >64=CCW)
11. **±0.01 BPM fine adjust granularity** — Precise tempo control without rapid jumps

---

## Next Steps (If User Continues)

1. **Hardware Testing**
   - Test MIDI in/out with real APC40 MK2
   - Verify CC numbers match controller firmware
   - Check LED colours on actual hardware

2. **Optional Enhancements**
   - Refine knob drag sensitivity if needed
   - Add web UI dark theme
   - Implement audio waveform visualizer

3. **C++/Qt Rewrite** (when ready)
   - Start with CMake scaffold
   - Get blank Qt window running
   - Gradually port Python logic

4. **Hardware Calibration**
   - Screen 4 reverse calibration
   - DM542T microstep verification

---

## Lessons Learned

- **Pixel sampling > guessing**: Measuring RGB bands from the image was more accurate than manual positioning
- **Optional imports need careful typing**: `Any` typing + guards make pyright happy
- **Radio-group logic needs clear state tracking**: Explicit `_*_pat_selected` variables beat implicit logic
- **Transparent UI > opaque widgets**: Canvas text overlays work better than blocking buttons
- **Revert early if visual result is poor**: The "animated fader caps" looked messy; yellow lines stayed clean
- **Modular code pays off**: 8 small modules are easier to navigate than 2400 lines
- **MIDI relative encoders need decoder knowledge**: APC40 uses 64=center encoding, not delta-tracking
- **Flash effects beat velocity dimming**: Button LEDs only support on/off; pulse effects impossible
- **Deferred initialization needed for complex layouts**: UI elements must be fully constructed before polling/updating
- **Aspect ratio matters visually**: Stretching canvas distorts controller image; correct ratio looks professional
- **Direct value decoding > magnitude multiplication**: For ±0.01 BPM, treat each message as single step, not scaled delta
- **Test encoder direction empirically**: CW/CCW can be inverted depending on hardware encoding; verify with user

---

**End of Project History**

Generated by Claude Sonnet 4.6 for the Cocktailbar BB Motorised Blinds Controller project.
