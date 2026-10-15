"""Read an app's front window from the macOS accessibility tree: its interactive elements and visible text."""

import time

import ApplicationServices as AS

# Roles worth acting on, with the short name Jev sees.
ROLES = {
    "AXButton": "button",
    "AXLink": "link",
    "AXTextField": "text field",
    "AXTextArea": "text area",
    "AXSearchField": "search field",
    "AXComboBox": "combo box",
    "AXCheckBox": "checkbox",
    "AXRadioButton": "radio button",
    "AXPopUpButton": "pop-up button",
    "AXMenuButton": "menu button",
    "AXMenuItem": "menu item",
    "AXTab": "tab",
    "AXSlider": "slider",
    "AXDisclosureTriangle": "disclosure",
}
TEXT_ROLES = {"AXTextField", "AXTextArea", "AXSearchField", "AXComboBox"}

MAX_NODES = 5000  # tree nodes visited per read
MAX_ELEMENTS = 150  # elements sent to Jev
MAX_TEXT_CHARS = 3000  # visible page text sent to Jev
MAX_SECONDS = 1.5

_enhanced = set()  # pids we've asked to expose their full tree


def _attr(element, name):
    err, value = AS.AXUIElementCopyAttributeValue(element, name, None)
    return value if err == AS.kAXErrorSuccess else None


def _point(value, kind):
    ok, v = AS.AXValueGetValue(value, kind, None)
    return v if ok else None


def _expose_full_tree(app, pid):
    """Chrome and Electron apps only build their web-content tree when asked."""
    if pid in _enhanced:
        return
    _enhanced.add(pid)
    AS.AXUIElementSetAttributeValue(app, "AXEnhancedUserInterface", True)  # Chrome, Safari
    AS.AXUIElementSetAttributeValue(app, "AXManualAccessibility", True)  # Electron
    time.sleep(0.3)  # give the app a moment to build the tree


def read_window(pid, region):
    """The app's front window, limited to what's inside `region` (global points).

    Returns (elements, text): the interactive elements as a list of
    {"role", "name", "left", "top", "width", "height"} in tree order, and the
    visible text (labels, paragraphs, prices...) in reading order.
    """
    app = AS.AXUIElementCreateApplication(pid)
    AS.AXUIElementSetMessagingTimeout(app, 1.0)
    _expose_full_tree(app, pid)
    window = _attr(app, "AXFocusedWindow") or (_attr(app, "AXWindows") or [None])[0]
    if window is None:
        return [], ""

    r_left, r_top = region["left"], region["top"]
    r_right, r_bottom = r_left + region["width"], r_top + region["height"]
    found, seen = [], set()
    lines, text_chars = [], 0
    # Each entry: (element, inside an interactive element whose name already covers its text).
    stack, visited, deadline = [(window, False)], 0, time.time() + MAX_SECONDS
    while (stack and visited < MAX_NODES and time.time() < deadline
           and (len(found) < MAX_ELEMENTS or text_chars < MAX_TEXT_CHARS)):
        el, in_control = stack.pop()
        visited += 1
        role = _attr(el, "AXRole")
        if role == "AXStaticText" and not in_control and text_chars < MAX_TEXT_CHARS:
            value = str(_attr(el, "AXValue") or "").strip()
            pos, size = _attr(el, "AXPosition"), _attr(el, "AXSize")
            pos = pos and _point(pos, AS.kAXValueCGPointType)
            size = size and _point(size, AS.kAXValueCGSizeType)
            visible = pos and size and size.width > 1 and size.height > 1
            if value and visible and r_left <= pos.x < r_right and r_top <= pos.y < r_bottom and (not lines or lines[-1] != value):
                lines.append(value)
                text_chars += len(value) + 1
        if role in ROLES and len(found) < MAX_ELEMENTS:
            pos, size = _attr(el, "AXPosition"), _attr(el, "AXSize")
            pos = pos and _point(pos, AS.kAXValueCGPointType)
            size = size and _point(size, AS.kAXValueCGSizeType)
            if pos and size and size.width > 2 and size.height > 2:
                cx, cy = pos.x + size.width / 2, pos.y + size.height / 2
                name = next((str(v).strip() for v in (
                    _attr(el, "AXTitle"), _attr(el, "AXDescription"),
                    _attr(el, "AXValue") if role in TEXT_ROLES else None,
                    _attr(el, "AXPlaceholderValue"), _attr(el, "AXHelp"),
                ) if v and str(v).strip()), "")
                key = (role, name, round(cx), round(cy))
                if (name or role in TEXT_ROLES) and r_left <= cx < r_right and r_top <= cy < r_bottom and key not in seen:
                    seen.add(key)
                    found.append({"role": ROLES[role], "name": name[:80], "left": pos.x, "top": pos.y,
                                  "width": size.width, "height": size.height})
        # Depth-first in reading order: push children reversed so the first child pops first.
        children = _attr(el, "AXChildren") or []
        stack.extend((child, in_control or role in ROLES) for child in reversed(children))
    return found, "\n".join(lines)[:MAX_TEXT_CHARS]


def page_loading(pid):
    """True while a browser's page is still loading (Chrome and Safari report AXLoaded on the web area)."""
    app = AS.AXUIElementCreateApplication(pid)
    AS.AXUIElementSetMessagingTimeout(app, 0.5)
    window = _attr(app, "AXFocusedWindow")
    queue, visited = [window] if window else [], 0
    while queue and visited < 500:  # the web area sits near the top of the tree
        el = queue.pop(0)
        visited += 1
        if _attr(el, "AXRole") == "AXWebArea":
            return _attr(el, "AXLoaded") is False
        queue.extend(_attr(el, "AXChildren") or [])
    return False


def unminimize_windows(pid):
    """Restore an app's minimized windows; returns how many were restored."""
    app = AS.AXUIElementCreateApplication(pid)
    restored = 0
    for window in _attr(app, "AXWindows") or []:
        if _attr(window, "AXMinimized"):
            AS.AXUIElementSetAttributeValue(window, "AXMinimized", False)
            restored += 1
    return restored
