# BRX automation

Automated synchronization of headpiece/dev, pinned dependency builds and Windows RTX5090 runtime releases.

Cloud builds publish prereleases. Optional self-hosted GPU qualification is required before promotion to a tested release. The pipeline preserves local changes and stops on merge conflicts.

CUDA13.3 and Actions are pinned. FFmpeg LGPL9 and vcpkg/curl refresh automatically. JIT-Gateway installation is separate.
