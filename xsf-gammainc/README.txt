Validation only, not an upstream PR or an accepted achievement.

The numerical bug was reported by chrisb83 in scipy/scipy#26471.
This pinned, AI-assisted candidate belongs in scipy/xsf after the human review
and submission required by SciPy's AI policy. No upstream issue or PR is opened
by this harness. It publishes test inputs and patch bytes for review.

The workflow uses standard ubuntu-24.04, read-only repository permissions,
Pixi 0.83.0, and the original project's locked environment and test commands.
It retains the original complete-suite result, proves the new regression fails
against unchanged production code, then applies the production patch and reruns
the full suite plus formatting. Existing baseline failures remain visible.
The source commit, patch SHA-256 values and both candidate Git blobs are checked.
