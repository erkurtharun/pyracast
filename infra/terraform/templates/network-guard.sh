#!/bin/bash
set -euo pipefail

# Host-side identity retrieval bypasses FORWARD; no container may use the VM identity.
iptables --wait --new-chain DOCKER-USER 2>/dev/null || iptables --wait --list DOCKER-USER >/dev/null
iptables-restore --wait --noflush <<'EOF'
*filter
:PYRACAST_METADATA - [0:0]
-F PYRACAST_METADATA
-A PYRACAST_METADATA -d 169.254.169.254/32 -j DROP
-A PYRACAST_METADATA -j RETURN
COMMIT
EOF
iptables --wait --check DOCKER-USER --jump PYRACAST_METADATA 2>/dev/null || \
  iptables --wait --insert DOCKER-USER 1 --jump PYRACAST_METADATA
iptables --wait --check FORWARD --jump DOCKER-USER 2>/dev/null || \
  iptables --wait --insert FORWARD 1 --jump DOCKER-USER
