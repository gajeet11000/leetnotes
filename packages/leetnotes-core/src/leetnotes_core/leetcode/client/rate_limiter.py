"""HTTP-level pacing for outbound LeetCode requests.

Split out from client.py so that module stays about *what* headers, cookies,
and retry policy the session uses — not *how* request pacing is implemented.
"""

import random
import time

from requests import PreparedRequest, Response
from requests_ratelimiter import LimiterAdapter


class JitteredLimiterAdapter(LimiterAdapter):
    """A rate-limiting adapter that also sleeps a small random amount per request.

    A flat requests-per-second cap alone still produces a perfectly regular
    request cadence, which is exactly the kind of mechanical pattern abuse
    detection heuristics key on during a large batch run (e.g. populating
    hundreds of solved problems in one sitting). The jitter breaks up that
    regularity without materially slowing anything down.
    """

    def __init__(self, *args, jitter_range: tuple[float, float] = (0.1, 0.6), **kwargs):
        super().__init__(*args, **kwargs)
        self.jitter_range = jitter_range

    def send(self, request: PreparedRequest, **kwargs) -> Response:
        time.sleep(random.uniform(*self.jitter_range))
        return super().send(request, **kwargs)
