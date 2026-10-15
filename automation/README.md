# BRX automation

The default branch is dev. Scheduled synchronization merges headpiece/dev into
a candidate while preserving local commits; it never force-pushes dev.
Source and mutable dependency assets are resolved into dependencies.lock.json
before a build. Neroued/master is recorded, not independently merged.

Cloud builds publish runtime-only prereleases. They are not equivalent to the
maintainer's original release gate or a GPU performance qualification.
With ENABLE_GPU_QUALIFICATION=true and an online Windows/rtx5090 runner,
the existing model-required randomized suite and the exact published binary
smoke test must pass before promotion/integration. This functional GPU test
does not run the historical Hermes workload. Evidence is qualification.json;
the original archive manifest intentionally remains a record of its hosted build.

CUDA13.3 and Actions are pinned. FFmpeg LGPL shared9 and vcpkg/curl refresh
automatically. A dependency/API/merge failure stops the candidate; previous dev
and previous releases remain available. Runtime installation into JIT-Gateway
is a separate operation and does not occur through this pipeline.

After a successful build, integration fast-forwards dev with force=false. It
requires no pull-request creation setting or personal token. A concurrent local
commit makes integration fail safely and is included in the next candidate.
This fork owns .github/workflows; upstream workflow edits are not imported.
Engine code and vendored dependencies are merged normally. Automation changes
also trigger a build; generated lock files do not cause endless rebuilds.
