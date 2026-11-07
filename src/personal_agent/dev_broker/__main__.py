"""Entry point: ``python -m personal_agent.dev_broker [--health]``.

``--health`` takes a light path that imports neither the broker nor the cost gate (FRE-1684).
"""

import sys

if "--health" in sys.argv[1:]:
    from personal_agent.dev_broker.health import main as health_main

    sys.exit(health_main(sys.argv[1:]))

from personal_agent.dev_broker.server import main  # noqa: E402

sys.exit(main(sys.argv[1:]))
