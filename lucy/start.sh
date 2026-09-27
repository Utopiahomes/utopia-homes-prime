#!/bin/sh
# Installs a Utopia Lucy profile (operator Lucy or guest Lucy, whichever the image carries) into
# HERMES_HOME on every start, then hands off to the Hermes image's own entrypoint (which must stay
# PID 1). The profile is replaced each time so a deploy always runs the reviewed settings;
# memories and sessions in HERMES_HOME are kept.
set -eu
HOME_DIR="${HERMES_HOME:-/opt/data}"
PROFILE=/opt/utopia-lucy/profile
mkdir -p "$HOME_DIR/plugins"
cp "$PROFILE/config.yaml" "$HOME_DIR/config.yaml"
cp "$PROFILE/SOUL.md" "$HOME_DIR/SOUL.md"
touch "$HOME_DIR/.no-bundled-skills"  # only the tools this profile enables
for plugin in "$PROFILE"/plugins/*/; do
  name=$(basename "$plugin")
  rm -rf "$HOME_DIR/plugins/$name"
  cp -R "$plugin" "$HOME_DIR/plugins/$name"
done
exec /opt/hermes/docker/entrypoint-dispatch.sh "$@"
