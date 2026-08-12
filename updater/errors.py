class UpdaterError(RuntimeError):
    code = "updater_error"


class RequestRejected(UpdaterError):
    code = "request_rejected"


class OperationInProgress(UpdaterError):
    code = "update_in_progress"


class StateError(UpdaterError):
    code = "invalid_operation_state"


class CompatibilityError(UpdaterError):
    code = "incompatible_release"


class CommandFailed(UpdaterError):
    code = "agent_command_failed"
