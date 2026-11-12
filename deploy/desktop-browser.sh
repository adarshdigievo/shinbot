#!/bin/sh
set -eu
binary=$SHINBOT_BROWSER_BINARY
uid=$SHINBOT_WORKSPACE_UID
gid=$SHINBOT_WORKSPACE_GID
unset SHINBOT_BROWSER_BINARY SHINBOT_WORKSPACE_UID SHINBOT_WORKSPACE_GID
exec setpriv --reuid "$uid" --regid "$gid" --clear-groups -- "$binary" "$@"
