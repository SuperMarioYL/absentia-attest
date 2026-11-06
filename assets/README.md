# Demo assets

`demo.gif` is a real terminal recording of the fixture archive. It is produced
by [vhs](https://github.com/charmbracelet/vhs) from `demo.tape`; nothing in the
GIF is staged or hand-edited.

## Re-render

Install `vhs`, `ttyd` and `ffmpeg`, then run from the repository root with the
project environment active (so `absentia` is on `PATH`):

```bash
python3.12 -m venv .venv
source .venv/bin/activate
pip install -e .
vhs assets/demo.tape
```

The tape writes `assets/demo.gif` (1100×520). On macOS:

```bash
brew install vhs ttyd ffmpeg
```

On Debian/Ubuntu:

```bash
sudo apt-get install -y vhs ttyd ffmpeg
```

The recorded run is lexical-only unless a local embedding endpoint is
configured. See `../docs/demo-results.json` for the exact commands, outputs and
hashes, and `../docs/demo/ledger-card.md` for the rendered ledger.
