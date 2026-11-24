#!/bin/sh
set -eu

# Store updates preserve the plugin home.  The deterministic archive gives
# sources mtime zero, so a same-size replacement can otherwise leave an older
# timestamp-based .pyc looking current.  Remove only this project's caches
# after UDX has deployed the new tree and before eisy-ui starts the plugin.
find src/eisy_onewire -type d -name __pycache__ -prune -exec rm -rf {} +

# When PG3x invokes this under the dedicated plugin identity, --user installs
# into that account's home rather than the system Python environment.
python3 -m pip install --user --requirement requirements.txt
