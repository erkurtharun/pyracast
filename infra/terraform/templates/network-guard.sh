#!/bin/bash
set -euo pipefail

# Only the Cloud SQL proxy needs the VM identity. Host-side secret retrieval bypasses FORWARD.
iptables --wait --new-chain DOCKER-USER 2>/dev/null || iptables --wait --list DOCKER-USER >/dev/null
iptables-restore --wait --noflush <<'EOF'
*filter
:PYRACAST_METADATA - [0:0]
-F PYRACAST_METADATA
-A PYRACAST_METADATA -s 172.30.80.3/32 -d 169.254.169.254/32 -j RETURN
-A PYRACAST_METADATA -d 169.254.169.254/32 -j DROP
-A PYRACAST_METADATA -j RETURN
COMMIT
EOF
iptables --wait --check DOCKER-USER --jump PYRACAST_METADATA 2>/dev/null || \
  iptables --wait --insert DOCKER-USER 1 --jump PYRACAST_METADATA
iptables --wait --check FORWARD --jump DOCKER-USER 2>/dev/null || \
  iptables --wait --insert FORWARD 1 --jump DOCKER-USER
