# systemone-computer-use

A computer-use agent for macOS driven by [TypeSafe Jev](https://typesafe.ai), a decision-only model. Give it a goal in plain English and it operates your Mac with the mouse and keyboard.

```
uv run python agent.py "play the last liked video on youtube"
```

## How it works

Jev can't write text. It only picks options with probabilities, answers yes/no, and draws boxes on images. So the work is split:

- **Planning** — a text model (Qwen 3.8 27B on Cerebras) breaks the goal into ordered sub-goals.
- **Each step** — the agent screenshots the target app's window and reads its buttons, links, fields and visible text from the macOS accessibility tree. Jev gets both, plus the plan and every step taken so far, and decides:
  - is the current sub-goal done?
  - which action next (click, type, go to URL, key, open app, scroll, drag, wait)?
  - which element to act on, with a box on the screenshot as fallback (and for a drag, where to release)?
- **Text** — when Jev decides something must be typed, the text model writes it.
- **Answer** — when the plan is done, the text model reports what was found.

The agent also waits for pages to load, refocuses the app if focus is lost, and avoids repeating actions that had no visible effect.

## Requirements

- macOS, Python 3.12+, [uv](https://docs.astral.sh/uv/)
- Your terminal needs **Screen Recording** and **Accessibility** permission (System Settings → Privacy & Security)
- API keys: Jev, Cerebras, and Anthropic (Claude Haiku is the fallback when both Cerebras models are rate limited)

## Setup

```
cp .env.template .env   # fill in the keys
uv sync
```

## Usage

```
uv run python agent.py "<goal>" [options]
```

| Option | Default | |
|---|---|---|
| `--launcher-hotkey` | `cmd+space` | Shortcut for Spotlight / Raycast / Alfred, used to launch apps that aren't running |
| `--max-steps` | 60 | Stop after this many steps |
| `--subgoal-steps` | 15 | Move to the next sub-goal after this many steps on one |
| `--done-threshold` | 0.8 | Probability at which Jev's "done" counts |
| `--text NAME=VALUE` | | A value the agent may type instead of generating one (repeatable) |
| `--history-images` | 0 | Previous screenshots to resend each step |
| `--model` | `jev-1.14.0` | Jev model |

Each run logs to `agent.log` and saves every step's screenshot to `runs/<timestamp>/`. Both may contain whatever was on your screen, so they are git-ignored.

## Caution

It drives your real mouse and keyboard and acts without asking, including on goals like "buy …". Watch it run. To stop, press Ctrl-C, or move the mouse into a corner of the main display. A blue glow around the pointer shows when the agent is in control.

It's experimental: expect misclicks and the occasional loop.
