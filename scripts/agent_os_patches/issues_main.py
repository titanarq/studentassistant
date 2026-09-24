"""`python -m agent_os.issues`, with the cheap board lookups of `board_lookup.py` installed first
(host-side workaround for titanarq/agent-os#27).

`-m` would run `issues.py` as a fresh `__main__` module, out of reach of any patch, so this imports
it as the ordinary module `agent_os.issues`, patches that, and calls its `main()` -- the same code
path, the same arguments, the same exit status. Run by the `python` wrapper beside this file
whenever it is asked for `-m agent_os.issues`; never needs to be called directly.
"""

import os
import sys

HERE = os.path.dirname(os.path.realpath(__file__))
# The main checkout's `agent_os/` package root (scripts/agent_os_patches/ -> scripts/ -> root).
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(HERE)), "agent_os"))
sys.path.insert(0, HERE)

import board_lookup  # noqa: E402
from agent_os import issues  # noqa: E402

board_lookup.install(issues)
sys.argv[0] = issues.__file__
issues.main()
