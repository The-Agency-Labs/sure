#!/bin/sh
# Attended sign-in, inside the scraper container (see attend.sh). Opens the
# bank's own Chrome profile on a virtual screen and shows that screen to the
# owner's web browser through noVNC, password-protected. Nothing is automated:
# the owner signs in, answers any reCAPTCHA or code, ticks "Remember me", and
# closes Chrome. The profile then carries that trust into nightly runs.
set -eu
BANK=$1
URL=$2
PROFILE=/srv/ingest-secrets/profiles/$BANK
: "${VNC_PASSWORD:?}"
export DISPLAY=:99 TZ="${INGEST_TIMEZONE:-UTC}" LANG="${INGEST_LOCALE:-en_US}.UTF-8"
LANG=$(echo "$LANG" | tr - _)

mkdir -p "$HOME" "$PROFILE"
rm -f "$PROFILE"/SingletonLock "$PROFILE"/SingletonCookie "$PROFILE"/SingletonSocket
Xvfb :99 -screen 0 1920x1080x24 -nolisten tcp &
sleep 1
x11vnc -storepasswd "$VNC_PASSWORD" "$HOME/vncpass" >/dev/null 2>&1
unset VNC_PASSWORD
x11vnc -display :99 -rfbauth "$HOME/vncpass" -localhost -rfbport 5900 -forever -shared -quiet -bg -o /dev/null
websockify --web /usr/share/novnc 0.0.0.0:6080 localhost:5900 >/dev/null 2>&1 &

# Same command line as the nightly runner (runner.chrome_command): keep in step.
google-chrome --user-data-dir="$PROFILE" --lang="${INGEST_LOCALE:-en-US}" --password-store=basic \
  --no-first-run --no-default-browser-check \
  --window-position=0,0 --window-size=1920,1080 "$URL" >/dev/null 2>&1
echo "Chrome closed; the $BANK profile is saved."
