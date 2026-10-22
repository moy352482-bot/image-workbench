"""Shared failure contracts. Must not import a feature or an adapter."""

class ConflictError(ValueError):
    """The requested change conflicts with the persisted state."""


class ComfyRejected(ValueError):
    """ComfyUI 明确拒绝了整次提交，没有接受的 prompt。"""


class ArtifactInvalid(ValueError):
    """生成已结束，但产物不满足交付条件。"""


class TextGenerationError(ValueError):
    """Text provider failure; uncertain executions must not be relaunched."""

    def __init__(self, message, *, uncertain=False):
        super().__init__(message)
        self.uncertain = uncertain
