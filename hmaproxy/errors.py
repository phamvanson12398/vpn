"""Exception hierarchy. Each error carries the process exit code the CLI returns."""


class HmaProxyError(Exception):
    exit_code = 1

    def __init__(self, message: str, exit_code: int | None = None):
        super().__init__(message)
        if exit_code is not None:
            self.exit_code = exit_code


class ConfigError(HmaProxyError):
    exit_code = 2


class DockerError(HmaProxyError):
    exit_code = 3


class CredentialError(HmaProxyError):
    exit_code = 4


class NotFoundError(HmaProxyError):
    exit_code = 5


class HealthError(HmaProxyError):
    exit_code = 6
