"""Bounded exponential retry state for restart-discoverable background work."""
import time


class BackgroundState:
    def __init__(self):
        self.jobs = {}

    def state(self, name):
        return self.jobs.setdefault(name, {"attempts": 0, "retry_at": 0, "running": False, "last_success": None})

    def ready(self, name):
        return time.time() >= self.state(name)["retry_at"]

    def begin(self, name):
        self.state(name)["running"] = True

    def success(self, name):
        self.state(name).update(attempts=0, retry_at=0, running=False, last_success=time.time())

    def failure(self, name):
        state = self.state(name)
        state["attempts"] += 1
        delay = min(300, 5 * 2 ** min(state["attempts"] - 1, 6))
        state.update(retry_at=time.time() + delay, running=False)
        return delay

    def cancel(self, name):
        self.state(name)["running"] = False

    def retry(self):
        for state in self.jobs.values():
            state.update(attempts=0, retry_at=0)

    def snapshot(self):
        return {name: {**state, "retry_in_seconds": max(0, round(state["retry_at"] - time.time()))}
                for name, state in self.jobs.items()}
