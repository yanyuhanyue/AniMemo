"""Latched no-network/no-child guard for the fixed userspace local test runner."""


class UserspaceEffectGuard:
    def __init__(self, allowed_child=()):
        self.violations = []
        self.allowed_child = tuple(allowed_child)
        self.child_used = False

    def audit(self, event, _args):
        if (
            event == "subprocess.Popen"
            and self.allowed_child
            and not self.child_used
            and len(_args) == 4
            and _args[0] == self.allowed_child[0]
            and type(_args[1]) in (tuple, list)
            and tuple(_args[1]) == self.allowed_child
        ):
            self.child_used = True
            return
        if event.startswith("socket.") or event in {
            "subprocess.Popen",
            "os.system",
            "os.posix_spawn",
            "os.posix_spawnp",
        }:
            self.violations.append({"event": event})
            raise RuntimeError("USERSPACE_TEST_EXTERNAL_EFFECT_BLOCKED")

    def require_clean(self):
        if self.violations:
            raise RuntimeError("USERSPACE_TEST_CAUGHT_EXTERNAL_EFFECT")
