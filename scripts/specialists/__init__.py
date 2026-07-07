"""Judgment specialists + the independence-protocol harness (ADR-0113 §3).

This package implements the *load-bearing safety property* of the self-driving
delivery loop: fresh-context specialists that review a worker's artifact
independently of master's framing, run from a fixed repo-checked template, treat
the artifact as untrusted data (prompt-injection-resistant), and can return a
blocking verdict master cannot override into a merge.

``harness`` is the reusable spine (reused by the measurement critic, the
doc-drift/deploy-verifier, and the autonomous-merge gate — FRE-833/834/835).
``pr_gate`` is the first specialist: the PR-gate reviewer.
"""
