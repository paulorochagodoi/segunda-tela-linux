"""X keysyms from bVNC to Linux evdev keycodes, plus the RFB wheel bits.

bVNC sends X keysyms (KeyboardEvent) and a pointer button mask. Bits 0–2 are
buttons. Bits 3–6 are the wheel and must not become a finger press.
Letters follow the unshifted evdev position (KEY_A for both 'a' and 'A');
the desktop layout turns that into a character. Shift is pressed only when
the keysym needs it and the client did not already send XK_Shift.
"""
from __future__ import annotations

KEY_ESC = 1
KEY_1, KEY_2, KEY_3, KEY_4, KEY_5 = 2, 3, 4, 5, 6
KEY_6, KEY_7, KEY_8, KEY_9, KEY_0 = 7, 8, 9, 10, 11
KEY_MINUS, KEY_EQUAL = 12, 13
KEY_BACKSPACE, KEY_TAB = 14, 15
KEY_Q, KEY_W, KEY_E, KEY_R, KEY_T = 16, 17, 18, 19, 20
KEY_Y, KEY_U, KEY_I, KEY_O, KEY_P = 21, 22, 23, 24, 25
KEY_LEFTBRACE, KEY_RIGHTBRACE = 26, 27
KEY_ENTER, KEY_LEFTCTRL = 28, 29
KEY_A, KEY_S, KEY_D, KEY_F, KEY_G = 30, 31, 32, 33, 34
KEY_H, KEY_J, KEY_K, KEY_L = 35, 36, 37, 38
KEY_SEMICOLON, KEY_APOSTROPHE, KEY_GRAVE = 39, 40, 41
KEY_LEFTSHIFT, KEY_BACKSLASH = 42, 43
KEY_Z, KEY_X, KEY_C, KEY_V = 44, 45, 46, 47
KEY_B, KEY_N, KEY_M = 48, 49, 50
KEY_COMMA, KEY_DOT, KEY_SLASH = 51, 52, 53
KEY_RIGHTSHIFT, KEY_LEFTALT, KEY_SPACE, KEY_CAPSLOCK = 54, 56, 57, 58
KEY_F1 = 59
KEY_F11, KEY_F12 = 87, 88
KEY_RIGHTCTRL, KEY_RIGHTALT = 97, 100
KEY_HOME, KEY_UP, KEY_PAGEUP = 102, 103, 104
KEY_LEFT, KEY_RIGHT, KEY_END = 105, 106, 107
KEY_DOWN, KEY_PAGEDOWN = 108, 109
KEY_INSERT, KEY_DELETE = 110, 111
KEY_LEFTMETA, KEY_RIGHTMETA = 125, 126

REL_HWHEEL, REL_WHEEL = 6, 8

# RFB pointer button-mask (community spec / bVNC).
BUTTON_MASK = 0x07
WHEEL_UP, WHEEL_DOWN = 1 << 3, 1 << 4
WHEEL_LEFT, WHEEL_RIGHT = 1 << 5, 1 << 6

_LETTERS = "qwertyuiopasdfghjklzxcvbnm"
_LETTER_CODES = (
    KEY_Q, KEY_W, KEY_E, KEY_R, KEY_T, KEY_Y, KEY_U, KEY_I, KEY_O, KEY_P,
    KEY_A, KEY_S, KEY_D, KEY_F, KEY_G, KEY_H, KEY_J, KEY_K, KEY_L,
    KEY_Z, KEY_X, KEY_C, KEY_V, KEY_B, KEY_N, KEY_M,
)
_DIGITS = "1234567890"
_DIGIT_CODES = (KEY_1, KEY_2, KEY_3, KEY_4, KEY_5, KEY_6, KEY_7, KEY_8, KEY_9, KEY_0)

# Unshifted glyph → keycode. Shifted glyph → same keycode with shift.
_PLAIN = {
    **{ord(ch): code for ch, code in zip(_LETTERS, _LETTER_CODES)},
    **{ord(ch): code for ch, code in zip(_DIGITS, _DIGIT_CODES)},
    ord(" "): KEY_SPACE,
    ord("-"): KEY_MINUS,
    ord("="): KEY_EQUAL,
    ord("["): KEY_LEFTBRACE,
    ord("]"): KEY_RIGHTBRACE,
    ord(";"): KEY_SEMICOLON,
    ord("'"): KEY_APOSTROPHE,
    ord("`"): KEY_GRAVE,
    ord("\\"): KEY_BACKSLASH,
    ord(","): KEY_COMMA,
    ord("."): KEY_DOT,
    ord("/"): KEY_SLASH,
}
_SHIFTED_GLYPH = {
    **{ord(ch.upper()): code for ch, code in zip(_LETTERS, _LETTER_CODES)},
    ord("!"): KEY_1,
    ord("@"): KEY_2,
    ord("#"): KEY_3,
    ord("$"): KEY_4,
    ord("%"): KEY_5,
    ord("^"): KEY_6,
    ord("&"): KEY_7,
    ord("*"): KEY_8,
    ord("("): KEY_9,
    ord(")"): KEY_0,
    ord("_"): KEY_MINUS,
    ord("+"): KEY_EQUAL,
    ord("{"): KEY_LEFTBRACE,
    ord("}"): KEY_RIGHTBRACE,
    ord(":"): KEY_SEMICOLON,
    ord('"'): KEY_APOSTROPHE,
    ord("~"): KEY_GRAVE,
    ord("|"): KEY_BACKSLASH,
    ord("<"): KEY_COMMA,
    ord(">"): KEY_DOT,
    ord("?"): KEY_SLASH,
}

# X keysyms bVNC actually emits for editing keys (RfbProto XK_* and X11).
_SPECIAL = {
    0xFF08: KEY_BACKSPACE,
    0xFF09: KEY_TAB,
    0xFF0D: KEY_ENTER,
    0xFF1B: KEY_ESC,
    0xFF50: KEY_HOME,
    0xFF51: KEY_LEFT,
    0xFF52: KEY_UP,
    0xFF53: KEY_RIGHT,
    0xFF54: KEY_DOWN,
    0xFF55: KEY_PAGEUP,
    0xFF56: KEY_PAGEDOWN,
    0xFF57: KEY_END,
    0xFF63: KEY_INSERT,
    0xFFFF: KEY_DELETE,
    0xFFE1: KEY_LEFTSHIFT,
    0xFFE2: KEY_RIGHTSHIFT,
    0xFFE3: KEY_LEFTCTRL,
    0xFFE4: KEY_RIGHTCTRL,
    0xFFE9: KEY_LEFTALT,
    0xFFEA: KEY_RIGHTALT,
    0xFFEB: KEY_LEFTMETA,
    0xFFEC: KEY_RIGHTMETA,
    0xFFE5: KEY_CAPSLOCK,
}
for _i in range(10):
    _SPECIAL[0xFFBE + _i] = KEY_F1 + _i
_SPECIAL[0xFFC8] = KEY_F11
_SPECIAL[0xFFC9] = KEY_F12

_SHIFT_CODES = {KEY_LEFTSHIFT, KEY_RIGHTSHIFT}


def keysym_to_linux(keysym: int) -> tuple[int, bool] | None:
    """`(keycode, needs_shift)` or None when this server has no key for it."""
    if keysym in _SPECIAL:
        return _SPECIAL[keysym], False
    if keysym in _PLAIN:
        return _PLAIN[keysym], False
    if keysym in _SHIFTED_GLYPH:
        return _SHIFTED_GLYPH[keysym], True
    return None


def used_keycodes() -> list[int]:
    codes = set(_PLAIN.values()) | set(_SHIFTED_GLYPH.values()) | set(_SPECIAL.values())
    return sorted(codes)


def plan_key(
    keysym: int,
    down: bool,
    *,
    shift_down: bool,
    synth_shift: bool,
) -> tuple[list[tuple[int, int]], bool, bool] | None:
    """Key events `(keycode, 0|1)` to write, plus the new shift flags.

    None means the keysym is not mapped. A shifted glyph presses Left Shift
    only when the client has not already sent XK_Shift.
    """
    mapped = keysym_to_linux(keysym)
    if mapped is None:
        return None
    code, needs_shift = mapped
    events: list[tuple[int, int]] = []
    value = 1 if down else 0
    if code in _SHIFT_CODES:
        shift_down = down
        synth_shift = False
        events.append((code, value))
        return events, shift_down, synth_shift
    if needs_shift and down and not shift_down and not synth_shift:
        events.append((KEY_LEFTSHIFT, 1))
        synth_shift = True
        shift_down = True
    events.append((code, value))
    if needs_shift and not down and synth_shift:
        events.append((KEY_LEFTSHIFT, 0))
        synth_shift = False
        shift_down = False
    return events, shift_down, synth_shift


def contact_buttons(mask: int) -> int:
    """Left/middle/right. Wheel bits are not a finger."""
    return mask & BUTTON_MASK


def wheel_delta(mask: int, previous: int) -> tuple[int, int]:
    """Rising edges of the wheel bits. `+dy` is wheel-up (REL_WHEEL positive)."""
    rose = mask & ~previous
    dy = (1 if rose & WHEEL_UP else 0) - (1 if rose & WHEEL_DOWN else 0)
    dx = (1 if rose & WHEEL_RIGHT else 0) - (1 if rose & WHEEL_LEFT else 0)
    return dx, dy
