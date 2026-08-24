class UpdaterError(RuntimeError):
    code = "updater_error"


class RequestRejected(UpdaterError):
    code = "request_rejected"


class OperationInProgress(UpdaterError):
    code = "update_in_progress"


class RecoveryRequired(UpdaterError):
    code = "manual_recovery_required"


class StateError(UpdaterError):
    code = "invalid_operation_state"


class CompatibilityError(UpdaterError):
    code = "incompatible_release"


class CommandFailed(UpdaterError):
    code = "agent_command_failed"

    def __init__(
        self,
        message: str,
        *,
        executable: str | None = None,
        stdout: str = "",
        stderr: str = "",
    ) -> None:
        super().__init__(message)
        self.executable = executable
        self.stdout = stdout
        self.stderr = stderr


class CommandTimedOut(CommandFailed):
    code = "agent_command_timeout"

    def __init__(
        self,
        executable: str,
        timeout_seconds: int,
        stdout: str,
        stderr: str,
    ) -> None:
        self.timeout_seconds = timeout_seconds
        super().__init__(
            f"command timed out: {executable}; timeoutSeconds={timeout_seconds}; "
            f"stdout={stdout}; stderr={stderr}",
            executable=executable,
            stdout=stdout,
            stderr=stderr,
        )


class CommandExited(CommandFailed):
    code = "agent_command_exit_failed"

    def __init__(
        self,
        executable: str,
        return_code: int,
        stdout: str,
        stderr: str,
    ) -> None:
        self.return_code = return_code
        super().__init__(
            f"command exited: {executable}; returnCode={return_code}; "
            f"stdout={stdout}; stderr={stderr}",
            executable=executable,
            stdout=stdout,
            stderr=stderr,
        )


class CommandStartFailed(CommandFailed):
    code = "agent_command_start_failed"

    def __init__(self, executable: str, failure_class: str) -> None:
        self.failure_class = failure_class
        super().__init__(
            f"command could not start: {executable}; failureClass={failure_class}",
            executable=executable,
        )
