"""Computer-use loop for the macOS desktop, driven by TypeSafe Jev.

At the start a text model breaks the goal into ordered sub-goals; Jev works
through them one at a time. Each step: make sure the app being worked on has
focus, screenshot its window,
read the window's interactive elements and visible text from the accessibility
tree, send them to
Jev along with the goal and every step taken so far, then act on the most
probable next action. Jev picks its target from the element list when it can
(exact position) and falls back to a box on the screenshot when it can't. When
the goal is done, a text model writes the answer (what was found) for the user.
Jev only returns typed decisions (choice / noul / box). When Jev decides new
text is needed, a text model writes it (Qwen, then gpt-oss on Cerebras, then
Claude Haiku when rate limited); otherwise Jev picks one of the
named --text values.
"""

import argparse
import base64
import glob
import io
import json
import logging
import math
import os
import re
import subprocess
import sys
import time
import uuid

import anthropic
import ax
import httpx
import mss
import pyautogui
import Quartz
from AppKit import (
    NSApplicationActivateAllWindows,
    NSApplicationActivationPolicyRegular,
    NSBundle,
    NSRunningApplication,
    NSWorkspace,
)
from dotenv import load_dotenv
from pyautogui._pyautogui_osx import keyboardMapping
from PIL import Image, ImageChops

API_URL = "https://api.typesafe.ai/v1/systemone"
CEREBRAS_URL = "https://api.cerebras.ai/v1/chat/completions"
GENERATE = "write_new_text"
NO_ELEMENT = "none_of_these"
TARGETED = ("click", "double_click", "right_click", "type", "scroll_up", "scroll_down")

log = logging.getLogger("agent")

# Jev image limits: at most 1568 px per side and 2,458,624 pixels. The docs say at
# least 256 px per side, but the API rejects anything under 280 (measured).
MIN_SIDE = 280
MAX_SIDE = 1568
MAX_PIXELS = 2_458_624

ACTIONS = {
    "click": "Left-click the target element. Never click something that would undo progress already made, such as pausing media that is already playing.",
    "double_click": "Double-click the target element.",
    "right_click": "Right-click the target element.",
    "type": "Replace the contents of the target input field on the page with text (any existing text is cleared first).",
    "go_to_url": "In a web browser, open a website or search the web: types a web address, or search words for a web search, into the browser's address bar and presses enter. Use this for anything beyond the site already open; a page's own search box only searches that site.",
    "key": "Press a keyboard key or shortcut (no clicking).",
    "open_app": "Bring an app the goal needs to the front: switches to it if it's in `running_apps`, otherwise launches it. Only when `frontmost_app` is not already that app.",
    "scroll_up": "Scroll the target area up by most of a screen.",
    "scroll_down": "Scroll the target area down by most of a screen, to see new content.",
    "wait": "Do nothing this step. Only when something is still loading (`page_loading` is true, or a spinner, progress bar or half-rendered page is visible), never because the next step is unclear.",
}

KEYS = {
    "enter": "Return / submit",
    "tab": "Move focus to the next field",
    "escape": "Close a dialog or cancel",
    "backspace": "Delete the previous character",
    "cmd+a": "Select all",
    "cmd+l": "Focus the browser address bar",
    "cmd+space": "Open the app launcher (Spotlight, Raycast...)",
    "up": "Arrow up",
    "down": "Arrow down",
}


APP_DIRS = [
    "/Applications",
    "/Applications/Utilities",
    "/System/Applications",
    "/System/Applications/Utilities",
    os.path.expanduser("~/Applications"),
]


def installed_apps():
    return sorted({os.path.basename(p)[:-4] for d in APP_DIRS for p in glob.glob(f"{d}/*.app")})


def running_instances(app):
    """Running copies of an installed app, looked up fresh (NSWorkspace's list goes stale)."""
    for d in APP_DIRS:
        bundle = NSBundle.bundleWithPath_(f"{d}/{app}.app")
        if bundle is not None and bundle.bundleIdentifier():
            return list(NSRunningApplication.runningApplicationsWithBundleIdentifier_(bundle.bundleIdentifier()))
    return []


def is_app(running, app):
    url = running.bundleURL() if running is not None else None
    return url is not None and url.path().endswith(f"/{app}.app")


# modifier name -> (event flag, key code)
MODIFIERS = {
    "command": (Quartz.kCGEventFlagMaskCommand, 55),
    "shift": (Quartz.kCGEventFlagMaskShift, 56),
    "option": (Quartz.kCGEventFlagMaskAlternate, 58),
    "ctrl": (Quartz.kCGEventFlagMaskControl, 59),
}


def post_key(keycode, down, flags):
    event = Quartz.CGEventCreateKeyboardEvent(None, keycode, down)
    Quartz.CGEventSetFlags(event, flags)
    Quartz.CGEventPost(Quartz.kCGHIDEventTap, event)
    time.sleep(0.03)


def hotkey(*keys):
    """Press a shortcut like a real keyboard: modifiers down, key, modifiers up.

    The key event carries the modifier flags, which system-wide shortcuts like
    cmd+space need. The modifiers are released explicitly afterwards; otherwise
    macOS keeps them held and every later keystroke becomes a shortcut
    (typing "Settings" turned into cmd+t, cmd+n... in the focused app).
    """
    *mods, key = keys
    flags = 0
    for m in mods:
        flags |= MODIFIERS[m][0]
        post_key(MODIFIERS[m][1], True, flags)
    post_key(keyboardMapping[key], True, flags)
    post_key(keyboardMapping[key], False, flags)
    for m in reversed(mods):
        flags &= ~MODIFIERS[m][0]
        post_key(MODIFIERS[m][1], False, flags)
    release_modifiers()


def release_modifiers():
    """Release any modifier macOS still thinks is held, so typed text stays plain text."""
    held = Quartz.CGEventSourceFlagsState(Quartz.kCGEventSourceStateCombinedSessionState)
    for flag, keycode in MODIFIERS.values():
        if held & flag:
            post_key(keycode, False, 0)


def floating_windows():
    """Ids of on-screen windows above normal app windows (launcher panels, menus, the Dock...)."""
    windows = Quartz.CGWindowListCopyWindowInfo(Quartz.kCGWindowListOptionOnScreenOnly, Quartz.kCGNullWindowID)
    return {w["kCGWindowNumber"] for w in windows if w.get("kCGWindowLayer", 0) > 0}


def wait_for(check, timeout=2.0):
    end = time.time() + timeout
    while time.time() < end:
        if check():
            return True
        time.sleep(0.1)
    return False


def frontmost_app():
    """The app that has keyboard focus, asked fresh from macOS each time.

    NSWorkspace.frontmostApplication() goes stale in a script with no run loop,
    and window order misses launcher panels (Raycast, Spotlight) that hold focus
    above normal windows.
    """
    asn = subprocess.run(["lsappinfo", "front"], capture_output=True, text=True).stdout.strip()
    info = subprocess.run(["lsappinfo", "info", "-only", "pid", asn], capture_output=True, text=True).stdout
    match = re.search(r"pid = (\d+)", info)
    return NSRunningApplication.runningApplicationWithProcessIdentifier_(int(match[1])) if match else None


def frontmost_is(app):
    return is_app(frontmost_app(), app)


def focus(app):
    """Bring an already-running app to the front, without the launcher."""
    for running in running_instances(app):
        running.activateWithOptions_(NSApplicationActivateAllWindows)
    if wait_for(lambda: frontmost_is(app)):
        return True
    # macOS may refuse activation requests from a background script; LaunchServices isn't refused.
    subprocess.run(["open", "-a", app], check=False)
    return wait_for(lambda: frontmost_is(app), timeout=5.0)


def has_visible_window(app):
    """Whether the app has a real (not popup-sized) window on screen."""
    windows = Quartz.CGWindowListCopyWindowInfo(
        Quartz.kCGWindowListOptionOnScreenOnly | Quartz.kCGWindowListExcludeDesktopElements,
        Quartz.kCGNullWindowID,
    )
    pids = {r.processIdentifier() for r in running_instances(app)}
    return any(
        w.get("kCGWindowLayer") == 0 and w["kCGWindowOwnerPID"] in pids
        and w["kCGWindowBounds"]["Width"] >= MIN_SIDE and w["kCGWindowBounds"]["Height"] >= MIN_SIDE
        for w in windows
    )


def ensure_window(app):
    """Make sure a focused app actually shows a window: it may be hidden, minimized or have none open."""
    if wait_for(lambda: has_visible_window(app), timeout=0.5):
        return True
    restored = 0
    for running in running_instances(app):
        running.unhide()
        restored += ax.unminimize_windows(running.processIdentifier())
    if wait_for(lambda: has_visible_window(app), timeout=1.5):
        log.info(f"  {app!r} had no window on screen; " + ("restored a minimized window" if restored else "unhid it"))
        return True
    # No windows open: a reopen request (what clicking the Dock icon sends) makes most apps open one.
    subprocess.run(["open", "-a", app], check=False)
    if wait_for(lambda: has_visible_window(app), timeout=3.0):
        log.info(f"  {app!r} had no window open; reopened it")
        return True
    if frontmost_is(app):
        hotkey("command", "n")
        if wait_for(lambda: has_visible_window(app), timeout=3.0):
            log.info(f"  {app!r} had no window open; opened one with cmd+n")
            return True
    log.info(f"  {app!r} still shows no window")
    return False


def capture_region(app):
    """What to screenshot: the app's main window, or with no app yet, the frontmost window's display.

    Capturing just the window keeps other apps (including the terminal showing
    this agent's log) out of view and makes the window's contents larger for Jev.
    The app's largest window is used, so popups, menus and sheets drawn over it
    are captured with it rather than instead of it.
    """
    with mss.MSS() as sct:
        monitors = sct.monitors[1:]

    def monitor_of(b):
        cx, cy = b["X"] + b["Width"] / 2, b["Y"] + b["Height"] / 2
        return next((m for m in monitors
                     if m["left"] <= cx < m["left"] + m["width"] and m["top"] <= cy < m["top"] + m["height"]), None)

    windows = [w for w in Quartz.CGWindowListCopyWindowInfo(
        Quartz.kCGWindowListOptionOnScreenOnly | Quartz.kCGWindowListExcludeDesktopElements,
        Quartz.kCGNullWindowID,
    ) if w.get("kCGWindowLayer") == 0]
    if not app:
        return next((m for m in (monitor_of(w["kCGWindowBounds"]) for w in windows) if m), monitors[0])

    best = None
    for w in windows:
        if not is_app(NSRunningApplication.runningApplicationWithProcessIdentifier_(w["kCGWindowOwnerPID"]), app):
            continue
        b, m = w["kCGWindowBounds"], monitor_of(w["kCGWindowBounds"])
        if m is None:
            continue
        # The window, clipped to its display.
        left, top = max(int(b["X"]), m["left"]), max(int(b["Y"]), m["top"])
        right = min(int(b["X"] + b["Width"]), m["left"] + m["width"])
        bottom = min(int(b["Y"] + b["Height"]), m["top"] + m["height"])
        region = {"left": left, "top": top, "width": right - left, "height": bottom - top}
        if min(fit_size(region["width"], region["height"])) < MIN_SIDE:
            continue  # too small for Jev once scaled
        if best is None or region["width"] * region["height"] > best["width"] * best["height"]:
            best = region
    return best or monitors[0]


def glide_path(start, end, rate=120):
    """Eased points from start to end: 0.15-0.6s depending on distance, `rate` points a second."""
    (x0, y0), (x1, y1) = start, end
    duration = min(0.6, max(0.15, math.hypot(x1 - x0, y1 - y0) / 2000))
    n = max(1, int(duration * rate))
    points = []
    for i in range(1, n + 1):
        t = i / n
        e = t * t * (3 - 2 * t)  # smoothstep: slow start, slow finish
        points.append((x0 + (x1 - x0) * e, y0 + (y1 - y0) * e))
    return points, duration / n


def mouse_move(x, y):
    """Glide the pointer to (x, y) instead of jumping there."""
    here = Quartz.CGEventGetLocation(Quartz.CGEventCreate(None))
    points, delay = glide_path((here.x, here.y), (x, y))
    for px, py in points:
        event = Quartz.CGEventCreateMouseEvent(None, Quartz.kCGEventMouseMoved, (px, py), Quartz.kCGMouseButtonLeft)
        Quartz.CGEventPost(Quartz.kCGHIDEventTap, event)
        time.sleep(delay)
    time.sleep(0.05)


def mouse_click(x, y, button="left", count=1):
    """Click with Quartz events; pyautogui clamps the pointer to the primary display."""
    if button == "left":
        down, up, btn = Quartz.kCGEventLeftMouseDown, Quartz.kCGEventLeftMouseUp, Quartz.kCGMouseButtonLeft
    else:
        down, up, btn = Quartz.kCGEventRightMouseDown, Quartz.kCGEventRightMouseUp, Quartz.kCGMouseButtonRight
    mouse_move(x, y)
    for n in range(1, count + 1):
        for kind in (down, up):
            event = Quartz.CGEventCreateMouseEvent(None, kind, (x, y), btn)
            Quartz.CGEventSetIntegerValueField(event, Quartz.kCGMouseEventClickState, n)
            Quartz.CGEventPost(Quartz.kCGHIDEventTap, event)
        time.sleep(0.05)


def mouse_scroll(x, y, pixels):
    """Scroll by `pixels` (positive is up) over (x, y), in small steps so it glides."""
    mouse_move(x, y)
    steps = 12
    for _ in range(steps):
        event = Quartz.CGEventCreateScrollWheelEvent(None, Quartz.kCGScrollEventUnitPixel, 1, round(pixels / steps))
        Quartz.CGEventPost(Quartz.kCGHIDEventTap, event)
        time.sleep(0.015)


def window_title():
    """Title of the focused app's front window (in a browser, the page title)."""
    front = frontmost_app()
    if front is None:
        return None
    windows = Quartz.CGWindowListCopyWindowInfo(
        Quartz.kCGWindowListOptionOnScreenOnly | Quartz.kCGWindowListExcludeDesktopElements,
        Quartz.kCGNullWindowID,
    )
    for w in windows:
        if w.get("kCGWindowLayer") == 0 and w["kCGWindowOwnerPID"] == front.processIdentifier() and w.get("kCGWindowName"):
            return w["kCGWindowName"]
    return None


def app_status():
    """Frontmost app and the running apps that have windows."""
    ws = NSWorkspace.sharedWorkspace()
    running = sorted(
        a.localizedName() for a in ws.runningApplications()
        if a.activationPolicy() == NSApplicationActivationPolicyRegular
    )
    front = frontmost_app()
    return (front.localizedName() if front else None), running


def fit_size(width, height):
    scale = min(1.0, MAX_SIDE / width, MAX_SIDE / height, (MAX_PIXELS / (width * height)) ** 0.5)
    return int(width * scale), int(height * scale)


def screenshot(region):
    """Capture a screen region, scaled to logical points and Jev's limits."""
    with mss.MSS() as sct:
        raw = sct.grab(region)
    img = Image.frombytes("RGB", raw.size, raw.bgra, "raw", "BGRX")
    img = img.resize(fit_size(region["width"], region["height"]), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=85)
    return buf.getvalue()


def build_request(model, goal, plan, subgoal_index, texts, apps, steps, history_images, current_image, title,
                  element_labels, loading=False, page_text="", tried=()):
    assets = {}

    def ref(jpeg):
        r = str(uuid.uuid4())
        assets[r] = {"type": "image", "data_base64": base64.b64encode(jpeg).decode(), "format": "jpeg"}
        return r

    state = {
        "today": time.strftime("%A %d %B %Y"),  # to judge words like "tomorrow" or "latest" in the goal and on the page
        "goal": goal,
        "plan": plan,  # ordered sub-goals for `goal`
        "completed_subgoals": plan[:subgoal_index],
        "current_subgoal": plan[subgoal_index],  # what to work on now
        "steps_so_far": steps or "none yet",
        "previous_screenshots": [
            {"taken_before_step": n, "screenshot": ref(img)} for n, img in history_images
        ],
        "current_screenshot": ref(current_image),
    }
    state["frontmost_app"], state["running_apps"] = app_status()
    state["current_window_title"] = title
    state["page_loading"] = loading  # the browser is still loading the page; what's shown may be the previous page
    state["page_text"] = page_text  # the window's visible text, read from the accessibility tree
    # Actions already tried on this page that changed nothing on screen: don't repeat them.
    state["tried_without_effect"] = sorted(tried)
    if texts:
        state["available_text"] = texts

    questions = {
        "done": {
            "type": "noul",
            "instructions": "Looking at `current_screenshot`, `page_text` (the window's visible text), `current_window_title` and the path taken in `steps_so_far` (each step's window title before and after), has `current_subgoal` been accomplished? Check details it asks for (dates, prices, names) in `page_text`.",
            "criteria": {
                "true": "The screen shows the end state `current_subgoal` asks for, e.g. the requested media is open and playing, the requested page or result is displayed, or a requested command has run and finished (its output is shown and the prompt is back).",
                "false": "More actions are still needed to reach the goal.",
            },
        },
        "action": {
            "type": "choice",
            "instructions": "Given `current_subgoal` (part of `goal`), `steps_so_far` and `current_screenshot`, what is the best next action toward `current_subgoal`?",
            "criteria": ACTIONS,
        },
        "target": {
            "type": "box",
            "instructions": "The single UI element in `current_screenshot` to click, type into or scroll over next to make progress on `current_subgoal`, given `steps_so_far`.",
        },
        "key": {
            "type": "choice",
            "instructions": "If the next action is pressing a key, which key best advances `current_subgoal`?",
            "criteria": KEYS,
        },
        "app": {
            "type": "choice",
            "instructions": "If the next action is opening an app, which installed app does `current_subgoal` need?",
            "criteria": {a: a for a in apps},
        },
    }
    if element_labels:
        questions["element"] = {
            "type": "choice",
            "instructions": "These are the clickable and typeable elements of the window in `current_screenshot`, read from its accessibility tree, with their centers as fractions of the screenshot. If the next action targets something on screen (click, type into, scroll over), which element is it? Check the screenshot to tell similar elements apart.",
            "criteria": {**element_labels, NO_ELEMENT: "The target is not in this list (use the box on the screenshot instead)."},
        }
    questions["text"] = {
        "type": "choice",
        "instructions": "If the next action is typing or going to a web address, should one of `available_text` be typed, or does new text need to be written?",
        "criteria": {**texts, GENERATE: "None of the available text fits this field; new text has to be written for it."},
    }
    return {"model": model, "state": state, "assets": assets, "questions": questions}


def screen_changed(before, after, min_pixels=500):
    """Whether two screenshots differ by more than a blinking cursor or clock tick.

    Counts sharply changed pixels: one new line of terminal output is ~2000,
    JPEG noise is 0, a cursor blink a few hundred.
    """
    a, b = (Image.open(io.BytesIO(x)).convert("L") for x in (before, after))
    if a.size != b.size:
        return True
    return ImageChops.difference(a, b).point(lambda p: 255 if p > 40 else 0).histogram()[255] > min_pixels


def element_box(element, region):
    """An accessibility element's rectangle as fractions of the screenshot, like Jev's box."""
    return {
        "left": (element["left"] - region["left"]) / region["width"],
        "right": (element["left"] + element["width"] - region["left"]) / region["width"],
        "top": (element["top"] - region["top"]) / region["height"],
        "bottom": (element["top"] + element["height"] - region["top"]) / region["height"],
    }


def describe(action, element, box, key):
    """A short name for what an action does, used to spot repeats that have no effect."""
    if action in TARGETED:
        if element:
            return f"{action} {element['role']} {element['name']!r}"
        return f"{action} at ({(box['left'] + box['right']) / 2:.2f}, {(box['top'] + box['bottom']) / 2:.2f})"
    if action == "key":
        return f"key {key}"
    return None


def ask_jev(client, body):
    resp = client.post(API_URL, json=body)
    if resp.is_error:
        raise RuntimeError(f"Jev {resp.status_code}: {resp.text}")
    return resp.json()["answers"]


TEXT_SYSTEM = "You write the text a computer-use agent types into an input field. Reply with only the exact text to type, nothing else."


def cerebras_text(client, model, prompt, image, system=TEXT_SYSTEM):
    """One Cerebras attempt; returns None when rate limited so the caller can fall back."""
    content = [{"type": "text", "text": prompt}]
    if image is not None:
        content.append({"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + base64.b64encode(image).decode()}})
    resp = client.post(CEREBRAS_URL, json={
        "model": model,
        "reasoning_effort": "medium",
        "temperature": 0.2,
        "max_tokens": 4096,
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": content}],
    })
    if resp.status_code == 429:
        return None
    if resp.is_error:
        raise RuntimeError(f"Cerebras {model} {resp.status_code}: {resp.text}")
    choice = resp.json()["choices"][0]
    if choice["finish_reason"] != "stop":
        raise RuntimeError(f"{model} stopped with {choice['finish_reason']}")
    return choice["message"]["content"].strip()


def haiku_text(client, prompt, image, system=TEXT_SYSTEM):
    response = client.messages.create(
        model="claude-haiku-5-5",
        max_tokens=1024,
        output_config={"effort": "low"},
        system=system,
        messages=[{"role": "user", "content": [
            *([{"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": base64.standard_b64encode(image).decode()}}] if image else []),
            {"type": "text", "text": prompt},
        ]}],
    )
    if response.stop_reason != "end_turn":
        raise RuntimeError(f"Claude stopped with {response.stop_reason}: {response.stop_details}")
    return "".join(b.text for b in response.content if b.type == "text").strip()


def write_text(cerebras, claude, prompt, image, system=TEXT_SYSTEM):
    """Qwen on Cerebras, then gpt-oss on Cerebras, then Claude Haiku.

    Each step down happens only on a rate limit. gpt-oss is text-only, so it gets
    no screenshot, just the prompt.
    """
    text = cerebras_text(cerebras, "qwen-3.8-27b", prompt, image, system)
    if text is None:
        log.info("  qwen-3.8-27b rate limited; using gpt-oss-120b (no screenshot)")
        text = cerebras_text(cerebras, "gpt-oss-120b", prompt, None, system)
    if text is None:
        log.info("  gpt-oss-120b rate limited; using claude-haiku-5-5")
        text = haiku_text(claude, prompt, image, system)
    return text


PLAN_SYSTEM = (
    "You plan tasks for a computer-use agent that operates a Mac with the mouse and keyboard, one small action "
    "at a time. Break the user's goal into the ordered sub-goals needed to complete it. Each sub-goal is one short "
    "line describing a state that can be checked on screen (for example: 'Chrome is open on flipkart.com', "
    "'Basmati rice is in the cart'). Spell out anything the goal implies, such as the individual items to find. "
    "Use 2 to 10 sub-goals. One per line, no numbering, nothing else."
)


def make_plan(cerebras, claude, goal):
    """Ordered sub-goals for the goal, written by the text model."""
    prompt = f"Today: {time.strftime('%A %d %B %Y')}\nGoal: {goal}"
    lines = write_text(cerebras, claude, prompt, None, PLAN_SYSTEM).splitlines()
    plan = [re.sub(r"^\s*(?:[-*•]|\d+[.)])\s*", "", line).strip() for line in lines]
    return [line for line in plan if line][:10] or [goal]


ANSWER_SYSTEM = (
    "You report the result of a computer-use task to the person who asked for it. Using the page text "
    "and screenshot, answer their goal in one to three short sentences with the specific details found "
    "(names, prices, dates, scores, addresses). If the goal was only an action, say briefly what was done. "
    "Plain text, no preamble."
)


def answer_goal(cerebras, claude, goal, plan, steps, title, page_text, image):
    """What the run found or did, written for the user."""
    prompt = (
        f"Today: {time.strftime('%A %d %B %Y')}\n"
        f"Goal: {goal}\n"
        f"Plan: {json.dumps(plan)}\n"
        f"Steps taken: {json.dumps(steps)}\n"
        f"Current window title: {title}\n"
        f"Visible page text:\n{page_text}"
    )
    return write_text(cerebras, claude, prompt, image, ANSWER_SYSTEM)


def generate_text(cerebras, claude, goal, subgoal, steps, title, image, box):
    """Write the exact text to type into a field."""
    if box is None:
        field = "The text is typed into the browser's address bar: a web address, or search words for a web search."
    else:
        where = {k: round(v, 3) for k, v in box.items()}
        field = f"The field to type into is at this box (fractions of the screenshot from the top-left): {json.dumps(where)}"
    prompt = (
        f"Goal: {goal}\n"
        f"Current sub-goal (write text for this): {subgoal}\n"
        f"Steps so far: {json.dumps(steps)}\n"
        f"Current window title: {title}\n"
        f"{field}"
    )
    return write_text(cerebras, claude, prompt, image)


def top(probabilities, n=3):
    ranked = sorted(probabilities.items(), key=lambda kv: kv[1], reverse=True)[:n]
    return ", ".join(f"{k}={v:.2f}" for k, v in ranked)


def execute(action, x, y, text, key, app, launcher_hotkey, page_height):
    if action == "click" or action == "type":
        mouse_click(x, y)
    elif action == "double_click":
        mouse_click(x, y, count=2)
    elif action == "right_click":
        mouse_click(x, y, button="right")
    if action == "type":
        hotkey("command", "a")
        release_modifiers()
        pyautogui.write(text, interval=0.02)
    elif action == "go_to_url":
        hotkey("command", "l")
        release_modifiers()
        pyautogui.write(text, interval=0.02)
        pyautogui.press("enter")
    elif action == "key":
        # pyautogui silently ignores "cmd" on macOS; it only knows "command".
        hotkey(*key.replace("cmd", "command").split("+"))
    elif action == "open_app" and running_instances(app):
        # Already running: switch to its window rather than launching it again.
        log.info(f"  {app!r} is already running; focusing its window")
        if not focus(app):
            raise RuntimeError(f"Could not bring {app!r} to the front")
        ensure_window(app)
    elif action == "open_app":
        before = floating_windows()
        hotkey(*launcher_hotkey.replace("cmd", "command").split("+"))
        # Never type unless a launcher (Spotlight, Raycast, Alfred...) really opened,
        # or the text lands in whatever has focus.
        if not wait_for(lambda: floating_windows() - before):
            raise RuntimeError(f"{launcher_hotkey} did not open an app launcher; not typing the app name")
        release_modifiers()
        pyautogui.write(app, interval=0.02)
        time.sleep(1.0)  # let the launcher's results load before picking the top one
        pyautogui.press("enter")
        if not wait_for(lambda: frontmost_is(app), timeout=5.0):
            log.info(f"  launcher did not bring {app!r} to the front; focusing it directly")
            if floating_windows() - before:
                pyautogui.press("escape")  # close the launcher so it stops taking keystrokes
        if not frontmost_is(app) and not focus(app):
            front = frontmost_app()
            raise RuntimeError(
                f"The launcher did not bring {app!r} to the front; "
                f"frontmost is {front.localizedName() if front else None!r}"
            )
        ensure_window(app)
    elif action in ("scroll_up", "scroll_down"):
        # Most of a screen: each step shows new content, with a little overlap for context.
        amount = int(page_height * 0.8)
        mouse_scroll(x, y, amount if action == "scroll_up" else -amount)


def run(args):
    load_dotenv()
    texts = dict(t.split("=", 1) for t in args.text)
    client = httpx.Client(
        timeout=120,
        headers={
            "Authorization": f"Bearer {os.environ['JEV_API_KEY']}",
            "TypeSafe-Danger-Zone-Images": "true",
        },
    )
    cerebras = httpx.Client(timeout=120, headers={"Authorization": f"Bearer {os.environ['CEREBRAS_API_KEY']}"})
    claude = anthropic.Anthropic()
    apps = installed_apps()
    steps, history_images = [], []
    target_app = None  # the app the agent last opened and is working in
    run_dir = os.path.join(args.save_dir, time.strftime("%Y%m%d-%H%M%S"))
    os.makedirs(run_dir, exist_ok=True)
    log.info(f"saving step screenshots to {run_dir}/")
    own_app = frontmost_app()  # the terminal running this script
    log.info(f"launched from {own_app.localizedName() if own_app else None!r}; never typing into it")
    plan = make_plan(cerebras, claude, args.goal)
    log.info("plan:\n" + "\n".join(f"  {i}. {g}" for i, g in enumerate(plan, 1)))
    sub, sub_steps = 0, 0  # current sub-goal, and steps spent on it
    no_effect, no_effect_page = set(), None  # actions that changed nothing, on the current page

    for n in range(1, args.max_steps + 1):
        if target_app and not frontmost_is(target_app):
            if not focus(target_app):
                raise RuntimeError(f"Could not bring {target_app!r} back to the front")
            log.info(f"  re-focused {target_app!r}")
        if target_app:
            ensure_window(target_app)
        region = capture_region(target_app)
        current = screenshot(region)
        title = window_title()
        front = frontmost_app()
        elements, page_text = ax.read_window(front.processIdentifier(), region) if front else ([], "")
        element_labels = {}
        for i, e in enumerate(elements, 1):
            fx_ = (e["left"] + e["width"] / 2 - region["left"]) / region["width"]
            fy_ = (e["top"] + e["height"] / 2 - region["top"]) / region["height"]
            element_labels[f"e{i}"] = f"{e['role']} {e['name']!r} at ({fx_:.2f}, {fy_:.2f})"
        with open(os.path.join(run_dir, f"step-{n:02d}.jpg"), "wb") as f:
            f.write(current)
        if steps:
            steps[-1]["window_after"] = title
            changed = title != steps[-1]["window_before"] or screen_changed(history_images[-1][1], current)
            steps[-1]["screen_changed"] = changed
            if not changed and steps[-1].get("did"):
                no_effect.add(steps[-1]["did"])
        if title != no_effect_page:
            no_effect, no_effect_page = set(), title
        recent = history_images[-args.history_images:] if args.history_images else []
        loading = bool(front) and ax.page_loading(front.processIdentifier())
        body = build_request(args.model, args.goal, plan, sub, texts, apps, steps, recent, current, title,
                             element_labels, loading, page_text, no_effect)
        answers = ask_jev(client, body)

        done = answers["done"]["noul"]
        log.info(f"[step {n} | sub-goal {sub + 1}/{len(plan)}] front={body['state']['frontmost_app']!r} title={title!r} elements={len(elements)} text={len(page_text)}ch done={done:.2f}  action: {top(answers['action']['probabilities'])}")
        if done >= args.done_threshold or sub_steps >= args.subgoal_steps:
            if done >= args.done_threshold:
                log.info(f"  sub-goal {sub + 1} done: {plan[sub]}")
            else:
                log.info(f"  sub-goal {sub + 1} not done after {sub_steps} steps; moving on: {plan[sub]}")
            sub, sub_steps = sub + 1, 0
            if sub == len(plan):
                log.info("Goal reached." if done >= args.done_threshold else "Plan finished.")
                log.info(f"Answer: {answer_goal(cerebras, claude, args.goal, plan, steps, title, page_text, current)}")
                return
            continue  # judge the same screen against the next sub-goal

        probabilities = answers["action"]["probabilities"]
        action = answers["action"]["choice"]
        stuck = (
            [s["action"] for s in steps[-2:]] == ["wait", "wait"]
            and not screen_changed(history_images[-2][1], current)
        )
        keyboard = ("type", "key", "go_to_url")
        if action in keyboard and own_app and front and front.processIdentifier() == own_app.processIdentifier():
            # Keystrokes here would land in the terminal running the agent (and enter would run them).
            action = max((a for a in probabilities if a not in keyboard), key=probabilities.get)
            log.info(f"  not typing into {own_app.localizedName()!r} (runs the agent); taking {action!r} instead")
        recent = steps[-4:]
        cycling = (
            len(recent) == 4
            and recent[0]["action"] == recent[2]["action"] != recent[1]["action"] == recent[3]["action"]
            and all(st.get("window_before") == title for st in recent)
        )
        if cycling and action in (recent[0]["action"], recent[1]["action"]):
            # Going back and forth (scroll up, down, up, down...) on the same page gets nowhere.
            loop = (recent[0]["action"], recent[1]["action"])
            action = max((a for a in probabilities if a not in loop), key=probabilities.get)
            log.info(f"  {loop[0]}/{loop[1]} repeating on the same page; taking {action!r} instead")
        if action == "wait" and stuck:
            # Jev falls back to waiting when unsure. Waiting is fine while the screen changes
            # (a command printing output); two waits on an unchanged screen means stuck.
            action = max((a for a in probabilities if a != "wait"), key=probabilities.get)
            log.info(f"  screen unchanged after two waits; taking next most likely action")
        jev_box = answers["target"]["box"]
        key = answers["key"]["choice"]
        # Targets in Jev's order of preference: elements (exact position from the accessibility
        # tree), with its box on the screenshot taking the place of none_of_these.
        if "element" in answers:
            ranked = sorted(answers["element"]["probabilities"].items(), key=lambda kv: kv[1], reverse=True)
            targets = [None if eid == NO_ELEMENT else elements[int(eid[1:]) - 1] for eid, _ in ranked]
        else:
            targets = [None]
        element = targets[0]
        box = element_box(element, region) if element else jev_box
        if describe(action, element, box, key) in no_effect:
            # Already tried on this page with no effect: take Jev's next choice that hasn't been.
            options = [(action, t) for t in targets[1:]] + [
                (a, targets[0]) for a in sorted(probabilities, key=probabilities.get, reverse=True) if a != action
            ]
            for alt_action, alt_element in options:
                alt_box = element_box(alt_element, region) if alt_element else jev_box
                if describe(alt_action, alt_element, alt_box, key) not in no_effect:
                    log.info(f"  {describe(action, element, box, key)} changed nothing before; trying {describe(alt_action, alt_element, alt_box, key) or alt_action}")
                    action, element, box = alt_action, alt_element, alt_box
                    break
        fx, fy = (box["left"] + box["right"]) / 2, (box["top"] + box["bottom"]) / 2
        x, y = region["left"] + fx * region["width"], region["top"] + fy * region["height"]
        via = f"{element['role']} {element['name']!r}" if element else "box on screenshot"
        app = answers["app"]["choice"]
        text = None
        if action in ("type", "go_to_url"):
            choice = answers["text"]["choice"]
            text = texts[choice] if choice != GENERATE else generate_text(
                cerebras, claude, args.goal, plan[sub], steps, title, current, box if action == "type" else None)

        detail = {
            "key": f"key={key}",
            "open_app": f"app={app!r}",
            "go_to_url": f"url={text!r}",
            "type": f"text={text!r} into {via} at ({x:.0f},{y:.0f})",
            "wait": "",
        }.get(action, f"on {via} at ({x:.0f},{y:.0f})")
        log.info(f"  -> {action} {detail}  (p={probabilities[action]:.2f})")

        execute(action, x, y, text, key, app, args.launcher_hotkey, region["height"])
        # A page that's still loading shows the old content; wait for it before the next screenshot.
        time.sleep(0.3)
        loading_front = frontmost_app()
        if loading_front and ax.page_loading(loading_front.processIdentifier()):
            loaded = wait_for(lambda: not ax.page_loading(loading_front.processIdentifier()), timeout=10.0)
            log.info("  waited for the page to load" if loaded else "  page still loading after 10s; continuing")

        step = {"step": n, "subgoal": sub + 1, "action": action, "window_before": title,
                "did": describe(action, element, box, key)}
        if action in TARGETED:
            step["at_fraction_of_screen"] = {"x": round(fx, 3), "y": round(fy, 3)}
            if element:
                step["target"] = f"{element['role']} {element['name']!r}"
        if action in ("type", "go_to_url"):
            step["typed"] = text
        if action == "key":
            step["key"] = key
        if action == "open_app":
            step["app"] = app
            target_app = app
        steps.append(step)
        history_images.append((n, current))
        sub_steps += 1
        time.sleep(args.wait)

    log.info(f"Stopped after {args.max_steps} steps without reaching the goal.")


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("goal", help="what the agent should accomplish")
    p.add_argument("--text", action="append", default=[], metavar="NAME=VALUE",
                   help="a value the agent may type (repeatable); otherwise text is generated")
    p.add_argument("--model", default=os.environ.get("JEV_MODEL", "jev-1.14.0"))
    p.add_argument("--launcher-hotkey", default="cmd+space",
                   help="shortcut for your app launcher: Spotlight, Raycast, Alfred... (e.g. option+space)")
    p.add_argument("--max-steps", type=int, default=60)
    p.add_argument("--subgoal-steps", type=int, default=15, help="steps to spend on one sub-goal before moving on")
    p.add_argument("--done-threshold", type=float, default=0.8)
    p.add_argument("--history-images", type=int, default=0,
                   help="how many previous screenshots to resend (all steps are always sent as text)")
    p.add_argument("--wait", type=float, default=1.0, help="seconds to wait after each action")
    p.add_argument("--log-file", default="agent.log", help="append the run log here as well as stderr")
    p.add_argument("--save-dir", default="runs", help="each run saves its step screenshots in a timestamped folder here")
    args = p.parse_args()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(message)s",
        handlers=[logging.StreamHandler(), logging.FileHandler(args.log_file)],
    )
    log.info(f"goal: {args.goal!r}")
    # Glow around the pointer while the agent controls it.
    # Excluded from screen capture, so it can stay on for the whole run.
    glow = subprocess.Popen([sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "glow.py")])
    try:
        run(args)
    except pyautogui.FailSafeException:
        log.info("Aborted: mouse moved to a screen corner.")
        sys.exit(1)
    except Exception:
        log.exception("Run failed")
        sys.exit(1)
    finally:
        glow.terminate()


if __name__ == "__main__":
    main()
