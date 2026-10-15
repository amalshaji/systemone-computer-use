"""Glowing outline around the mouse pointer while the agent is in control.

Runs as its own process (drawing needs an AppKit run loop). The window is
excluded from screen capture, so Jev's screenshots never show it.
"""


import objc
from AppKit import (
    NSApplication,
    NSApplicationActivationPolicyProhibited,
    NSBackingStoreBuffered,
    NSColor,
    NSCompositingOperationSourceOver,
    NSCursor,
    NSEvent,
    NSShadow,
    NSScreenSaverWindowLevel,
    NSTimer,
    NSView,
    NSWindow,
    NSWindowCollectionBehaviorCanJoinAllSpaces,
    NSWindowCollectionBehaviorFullScreenAuxiliary,
    NSWindowCollectionBehaviorStationary,
    NSWindowSharingNone,
    NSWindowStyleMaskBorderless,
)
from Foundation import NSObject

PAD = 18  # room around the pointer image for the glow to spread


def current_cursor():
    return NSCursor.currentSystemCursor() or NSCursor.arrowCursor()


class GlowView(NSView):
    """Draws the current pointer image with a blurred blue shadow.

    The real pointer is always drawn above every window, so it covers the copy
    and only the glowing outline around it shows.
    """

    cursor = None

    def drawRect_(self, rect):
        if self.cursor is None:
            return
        shadow = NSShadow.alloc().init()
        shadow.setShadowColor_(NSColor.colorWithCalibratedRed_green_blue_alpha_(0.2, 0.6, 1.0, 1.0))
        shadow.setShadowBlurRadius_(12)
        shadow.setShadowOffset_((0, 0))
        shadow.set()
        for _ in range(6):  # stacked passes make the halo bright enough to see on any background
            self.cursor.image().drawAtPoint_fromRect_operation_fraction_(
                (PAD, PAD), ((0, 0), (0, 0)), NSCompositingOperationSourceOver, 1.0
            )


class Follower(NSObject):
    def initWithWindow_(self, window):
        self = objc.super(Follower, self).init()
        self.window = window
        return self

    def tick_(self, timer):
        cursor = current_cursor()
        size, hot = cursor.image().size(), cursor.hotSpot()
        loc = NSEvent.mouseLocation()
        # Hot spot is measured from the image's top-left; window origins are bottom-left.
        origin = (loc.x - hot.x - PAD, loc.y - (size.height - hot.y) - PAD)
        self.window.setFrame_display_((origin, (size.width + 2 * PAD, size.height + 2 * PAD)), False)
        view = self.window.contentView()
        view.setFrame_(((0, 0), (size.width + 2 * PAD, size.height + 2 * PAD)))
        view.cursor = cursor
        view.setNeedsDisplay_(True)


def main():
    app = NSApplication.sharedApplication()
    # Background-only: no Dock icon, and never the front app, even when launched from
    # the front terminal (the agent decides what to do from which app is in front).
    app.setActivationPolicy_(NSApplicationActivationPolicyProhibited)

    window = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
        ((0, 0), (1, 1)), NSWindowStyleMaskBorderless, NSBackingStoreBuffered, False
    )
    window.setOpaque_(False)
    window.setBackgroundColor_(NSColor.clearColor())
    window.setHasShadow_(False)
    window.setIgnoresMouseEvents_(True)
    window.setLevel_(NSScreenSaverWindowLevel)
    window.setCollectionBehavior_(
        NSWindowCollectionBehaviorCanJoinAllSpaces
        | NSWindowCollectionBehaviorStationary
        | NSWindowCollectionBehaviorFullScreenAuxiliary
    )
    window.setSharingType_(NSWindowSharingNone)  # excluded from screen capture
    window.setContentView_(GlowView.alloc().initWithFrame_(((0, 0), (1, 1))))

    follower = Follower.alloc().initWithWindow_(window)
    follower.tick_(None)
    window.orderFrontRegardless()
    NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(1 / 60, follower, "tick:", None, True)

    app.run()


if __name__ == "__main__":
    main()
