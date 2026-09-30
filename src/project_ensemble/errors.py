class EnsembleError(Exception):
    """Base error."""

class AccessDeniedError(EnsembleError):
    pass

class PolicyNotConfiguredError(EnsembleError):
    pass

class FeatureNotImplementedError(EnsembleError):
    """A deliberately exposed interface has no executable state machine yet."""

    pass

class ResearchQualityControlError(EnsembleError):
    """A claim cannot produce a trustworthy evidence packet but the meeting may continue."""

    def __init__(self, code: str, summary: str):
        super().__init__(summary)
        self.code = code
        self.summary = summary

class ResearchRequestRejectedError(ValueError, EnsembleError):
    """A Research Desk request is not claim-scoped and may be rewritten."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason

class ImmutableWriteError(EnsembleError):
    pass

class BallotError(EnsembleError):
    pass

class BallotNotClosedError(BallotError):
    pass

class InvalidBallotError(BallotError):
    pass

class RepresentativeUnavailableError(EnsembleError):
    pass

class ProviderError(EnsembleError):
    pass

class TransientProviderError(ProviderError):
    def __init__(self, message: str, *, retry_after_seconds: float | None = None):
        super().__init__(message)
        self.retry_after_seconds = retry_after_seconds


class OpenAlexDailyQuotaExhausted(TransientProviderError):
    """The OpenAlex daily credit budget is exhausted until its UTC reset."""

    def __init__(self, message: str, *, reset_seconds: float):
        super().__init__(message, retry_after_seconds=reset_seconds)
        self.reset_seconds = reset_seconds


class OpenAlexRateLimited(TransientProviderError):
    """OpenAlex returned 429 without enough evidence to classify a daily limit."""


class OpenAlexConnectionUnavailable(TransientProviderError):
    """OpenAlex could not be reached after a transport or service failure."""

class OpenAlexQueryRejected(ProviderError):
    """OpenAlex rejected the search request; changing the model cannot fix it."""

class PermanentProviderError(ProviderError):
    pass

class ProviderContentRejectedError(PermanentProviderError):
    """A provider rejected this request's content, not the provider as a whole."""

    def __init__(self, message: str, *, status_code: int, provider_request_id: str | None = None):
        super().__init__(message)
        self.status_code = status_code
        self.provider_request_id = provider_request_id

class OutputLimitReachedError(EnsembleError):
    pass

class InputContextLimitError(EnsembleError):
    pass

class EmptyModelOutputError(EnsembleError):
    """The provider completed a request without returning final answer text."""

    pass

class ModelReplacementRequested(EnsembleError):
    """Interactive Ctrl-R requested a safe model handoff at a call boundary."""

    pass

class EventChainError(EnsembleError):
    pass

class NotificationConfigurationError(EnsembleError):
    pass

class NotificationDeliveryError(EnsembleError):
    pass
