#!/bin/bash
set -euo pipefail
service_pid=""
cleanup() {
    trap - EXIT
    if [ -n "$service_pid" ]; then
        kill -TERM "$service_pid" 2>/dev/null || true
        for attempt in {1..40}; do
            kill -0 "$service_pid" 2>/dev/null || break
            sleep 0.2
        done
    fi
    kill $(jobs -p) 2>/dev/null || true
    wait || true
}
trap cleanup EXIT
trap 'exit 143' TERM INT
umask 077
mkdir -p /data
if [ ! -f /data/view-password ]; then
    python -c 'import secrets; from pathlib import Path; Path("/data/view-password").write_text(secrets.token_hex(4))'
fi
vncpasswd -f < /data/view-password > /data/vnc-password
Xvfb :99 -screen 0 1280x800x24 -nolisten tcp &
for attempt in 1 2 3 4 5 6 7 8 9 10; do
    [ -S /tmp/.X11-unix/X99 ] && break
    sleep 1
done
X0tigervnc -display :99 -rfbport 5900 -localhost -SecurityTypes VncAuth -PasswordFile /data/vnc-password -AlwaysShared -AcceptKeyEvents=0 -AcceptPointerEvents=0 -AcceptSetDesktopSize=0 > /tmp/vnc-view.log 2>&1 &
X0tigervnc -display :99 -rfbport 5901 -localhost -SecurityTypes VncAuth -PasswordFile /data/vnc-password -AlwaysShared -AcceptKeyEvents=1 -AcceptPointerEvents=1 -AcceptSetDesktopSize=0 > /tmp/vnc-control.log 2>&1 &
websockify 127.0.0.1:6080 127.0.0.1:5900 > /tmp/ws-view.log 2>&1 &
websockify 127.0.0.1:6081 127.0.0.1:5901 > /tmp/ws-control.log 2>&1 &
uvicorn shinbot.services.browser:app --host 0.0.0.0 --port 8083 --no-access-log &
service_pid=$!
wait -n
exit 1
