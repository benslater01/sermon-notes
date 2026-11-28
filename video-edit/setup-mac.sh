#!/bin/bash
# One-time setup on the Mac. Safe to re-run (also updates the tools).
#   bash setup-mac.sh                       finds the plugged-in external drive and uses it
#   bash setup-mac.sh "/Volumes/YourDrive"  use this drive (or any folder)
# Tools (Python env + this repo) always live on the internal disk in ~/.firstnaz-video:
# external drives (exFAT) can't hold a Python env, and edits should still start if the drive is renamed.
set -e
DRIVE="${1%/}"
if [ -z "$DRIVE" ]; then
  # find plugged-in external drives (USB/Thunderbolt), skipping the Mac's own disk and disk images
  EXT=()
  for v in /Volumes/*; do
    [ -d "$v" ] && [ ! -L "$v" ] || continue
    diskutil info "$v" 2>/dev/null | grep -Eq "Device Location: +External|Protocol: +(USB|Thunderbolt)" || continue
    diskutil info "$v" 2>/dev/null | grep -Eq "Protocol: +Disk Image" && continue
    EXT+=("$v")
  done
  if [ ${#EXT[@]} -eq 1 ]; then DRIVE="${EXT[0]}"; echo "Using external drive: $DRIVE"
  elif [ ${#EXT[@]} -gt 1 ]; then
    echo "Several external drives found:"; for i in "${!EXT[@]}"; do echo "  $((i+1))) ${EXT[$i]}"; done
    read -r -p "Which one holds the video projects? " n < /dev/tty; DRIVE="${EXT[$((n-1))]}"
  else
    echo "No external drive found. Plug it in and run this again (or pass a path)."; exit 1
  fi
fi
if [ -n "$DRIVE" ]; then
  [ -d "$DRIVE" ] || { echo "$DRIVE not found. Plug in the drive, or check the name in Finder (it's /Volumes/<drive name>)."; exit 1; }
  ROOT="$DRIVE/FirstNaz Video"
else
  ROOT="$HOME/FirstNaz Video"
fi
TOOLS="$HOME/.firstnaz-video"
mkdir -p "$ROOT/projects" "$ROOT/music" "$TOOLS"
for b in /opt/homebrew/bin/brew /usr/local/bin/brew; do [ -x "$b" ] && eval "$("$b" shellenv)" && break; done
command -v brew >/dev/null || { echo "Install Homebrew first: https://brew.sh"; exit 1; }
# ffmpeg-full (keg-only) has drawtext + libass for outro text, name labels and captions; plain ffmpeg lacks them
brew list ffmpeg-full >/dev/null 2>&1 || brew install ffmpeg-full
brew list python@3.12 >/dev/null 2>&1 || brew install python@3.12
if [ -d "$TOOLS/firstnaz-skills/.git" ]; then git -C "$TOOLS/firstnaz-skills" pull --ff-only
else git clone https://github.com/benslater01/firstnaz-skills.git "$TOOLS/firstnaz-skills"; fi
[ -d "$TOOLS/venv" ] || "$(brew --prefix python@3.12)/bin/python3.12" -m venv "$TOOLS/venv"
"$TOOLS/venv/bin/pip" install -q --upgrade pip numpy mlx-whisper google-api-python-client google-auth-oauthlib
# warm the Whisper model once (about 1.5 GB download) so the first edit doesn't stall
"$TOOLS/venv/bin/python" -c "from huggingface_hub import snapshot_download; snapshot_download('mlx-community/whisper-large-v3-turbo')"
# the wrapper sits next to the projects and points the tool at its own folder
cat > "$ROOT/edit" <<'SH'
#!/bin/bash
export FIRSTNAZ_VIDEO_ROOT="$(cd "$(dirname "$0")" && pwd)"
export PATH="/opt/homebrew/opt/ffmpeg-full/bin:/usr/local/opt/ffmpeg-full/bin:/opt/homebrew/bin:/usr/local/bin:$PATH"
exec "$HOME/.firstnaz-video/venv/bin/python" "$HOME/.firstnaz-video/firstnaz-skills/video-edit/edit.py" "$@"
SH
chmod +x "$ROOT/edit"
# fill the music library (curated, free, safe for YouTube/social); skips tracks already there
"$ROOT/edit" music seed || echo "Music download had trouble; run \"$ROOT/edit\" music seed later."
echo "Ready. Projects: $ROOT/projects   Music: $ROOT/music"
echo "In the Claude desktop app, add \"$ROOT\" as a Remote Control folder (Settings, Claude Code, Remote Control)."
