class AgentSmithError(Exception):
    """Exception de base pour tout le projet Agent Smith."""
    pass


class SandboxError(AgentSmithError):
    """Exception de base pour les erreurs liées à la sandbox."""
    pass


class UnauthorizedImportError(SandboxError):
    """Levée lorsqu'un module non autorisé tente d'être importé."""
    pass


class ForbiddenNetworkError(SandboxError):
    """Levée lors d'une tentative d'accès réseau non autorisée."""
    pass

class FinalAnswerException(SandboxError):
    """Exception interne pour intercepter l'appel à final_answer."""
    def __init__(self, answer: str):
        self.answer = answer
