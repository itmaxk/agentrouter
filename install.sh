#!/bin/sh
# Installs the `agentrouter` command (no pip needed) and the systemd user service.
set -eu
here=$(cd "$(dirname "$0")" && pwd)
python=$(command -v python3)
mkdir -p "$HOME/.local/bin" "$HOME/.config/systemd/user"
cat > "$HOME/.local/bin/agentrouter" <<SH
#!/bin/sh
PYTHONPATH="$here\${PYTHONPATH:+:\$PYTHONPATH}" exec "$python" -m agentrouter "\$@"
SH
chmod +x "$HOME/.local/bin/agentrouter"
cp "$here/systemd/agentrouter.service" "$HOME/.config/systemd/user/agentrouter.service"
systemctl --user daemon-reload 2>/dev/null || true
echo "installed $HOME/.local/bin/agentrouter"
echo "start with: systemctl --user enable --now agentrouter"
