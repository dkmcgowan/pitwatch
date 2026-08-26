"""Turning readings into runs, and runs into things worth saying.

Constants here are the ones that describe how the detector works rather than
what this pit is like. The difference is the test for whether something belongs
on the settings page: "how many amps does this motor draw" is a fact about the
building and nobody but the owner can answer it, while "how long a dip in
current is still the same run" is a fact about sampling and offering it as a
box only invites a wrong answer.
"""

from __future__ import annotations

# How long the current has to stay below the running threshold before the run
# is treated as over.
#
# There has to be some hold. The Shelly pushes about once a second and these
# pumps run three or four seconds, so a single low reading in the middle of a
# run would otherwise end it and start another, turning one four second run
# into two two second ones. Two runs a moment apart is exactly the shape short
# cycling has, so the cost of getting this wrong is not a cosmetic one: it is a
# false alarm about a check valve that is fine.
#
# A second bridges a dropped or low reading and is far shorter than the gap
# between two genuinely separate calls, which is the pit refilling.
RUN_STOP_HOLD_MS = 1000
