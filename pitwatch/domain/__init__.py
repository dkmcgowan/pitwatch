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


# Throw away the first reading of every run.
#
# This replaces a settable inrush window measured in milliseconds, which could
# never have worked: it assumed readings arrive fast enough that a fraction of
# a second at the start of a run contains several, and they do not. The meter
# reports on its own schedule and a four second run produces two readings.
#
# What eleven hours of real readings show is much simpler. The surge lands in
# the first reading of a run and nowhere else. Across 73 runs the first reading
# had a median of 16.5 A against 15.2 A for every reading after it, and the
# handful of runs that caught a real surge saw 20 to 40 A, always in that first
# reading. So the rule is not a duration at all. It is: the first reading of a
# run is the start of a motor and the rest are the motor working, and only the
# rest describe what it draws.
#
# The peak is still recorded, separately and including the surge, because a
# starting surge climbing month over month is a bearing on its way out.
DISCARD_FIRST_READING = True


# The line between a pump that is off and a pump that is running, in amps.
#
# This was a setting, and it should not have been. The test at the top of this
# file is whether a number describes the building or the measurement, and this
# one turned out to describe neither: on eleven hours of real readings an idle
# clamp reads 0.000 exactly, a running one reads about 15, and every threshold
# from 0.2 A to 10 A found the same 73 runs. There is no judgement to make, so
# there is no reason to ask anybody to make it.
#
# Not zero, though. That is margin against a control transformer sharing the
# conductor, which would put a small standing current on a clamp that is
# otherwise reading nothing.
RUNNING_AMPS = 1.0
