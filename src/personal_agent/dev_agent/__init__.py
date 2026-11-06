"""The development agent's container boundary (ADR-0159; FRE-1682).

The spec of the agent, check and handoff containers is data in
``config/dev_agent/container_profiles.yaml``. :mod:`container_spec` loads it and renders
``docker create`` arguments. :mod:`verification` compares live containers with it.
"""
