
import os as _os

# Times without an offset are local time (timeutil). Pin the zone so
# results do not depend on the machine running the tests.
_os.environ["LR_TIMEZONE"] = "Europe/London"
