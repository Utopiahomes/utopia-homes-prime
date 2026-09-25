#!/bin/sh
# Installs Utopia Lucy's profile into HERMES_HOME on every start, then hands off to the Hermes
# image's own entrypoint (which must stay PID 1). The profile is replaced each time so a deploy
# always runs the reviewed settings; memories and sessions in HERMES_HOME are kept.
set -eu
HOME_DIR="${HERMES_HOME:-/opt/data}"
PROFILE=/opt/utopia-lucy/profile
mkdir -p "$HOME_DIR/plugins"
cp "$PROFILE/config.yaml" "$HOME_DIR/config.yaml"
cp "$PROFILE/SOUL.md" "$HOME_DIR/SOUL.md"
touch "$HOME_DIR/.no-bundled-skills"  # only the tools this profile enables
rm -rf "$HOME_DIR/plugins/utopia_business"
cp -R "$PROFILE/plugins/utopia_business" "$HOME_DIR/plugins/utopia_business"
exec /opt/hermes/docker/entrypoint-dispatch.sh "$@"
