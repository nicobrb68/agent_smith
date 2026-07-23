class AgentSmithError(Exception):
    """Base exception for the whole Agent Smith project."""
    pass


class SandboxError(AgentSmithError):
    """Base exception for sandbox-related errors."""
    pass


class UnauthorizedImportError(SandboxError):
    """Raised when an unauthorized module is imported."""
    pass


class ForbiddenNetworkError(SandboxError):
    """Raised when an unauthorized network access is attempted."""
    pass


class PathAccessError(SandboxError):
    """Raised when code tries to access a path outside allowed directories."""
    pass


class FinalAnswerException(SandboxError):
    """Internal exception used to intercept the final_answer call."""
    def __init__(self, answer: str):
        self.answer = answer
