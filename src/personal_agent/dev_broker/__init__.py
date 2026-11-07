"""The development-agent broker (ADR-0159 D4.3, D4.4, D5; FRE-1683).

One service with two listeners. On ``dev-agent`` it serves the agent's only model route, metered
against a per-job and a per-month cap. On ``cloud-sim`` it serves the control relay, which only the
gateway can call and which forwards only to the current job's OpenCode server.
"""
