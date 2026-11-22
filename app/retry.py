class RetryNeeded(Exception):
    """Transient or currently unresolved work; retry without occupying a worker."""


class WorkUnavailable(Exception):
    """The platform explicitly disallows this work; API retries cannot fix it."""


class PointRecoveryNeeded(RetryNeeded):
    """Wait for point cooldowns without consuming a course-wide retry budget."""
    def __init__(self, reason, next_retry, pending=0, blocked=0):
        super().__init__(reason)
        self.next_retry = next_retry
        self.pending = pending
        self.blocked = blocked
