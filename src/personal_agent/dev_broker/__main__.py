"""Entry point: ``python -m personal_agent.dev_broker [--health]``."""

import sys

from personal_agent.dev_broker.server import main

sys.exit(main(sys.argv[1:]))
