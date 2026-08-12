"""AniMemo host update agent.

The package is deliberately independent from Django.  Its public seam is the
local RPC protocol; Docker and release-filesystem authority stay inside the
host process.
"""

__version__ = "1.0.0"
