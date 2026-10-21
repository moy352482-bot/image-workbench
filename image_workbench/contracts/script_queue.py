"""Worker request schemas."""
from pydantic import Field
from .scripts import ScriptBody, ID

class Capability(ScriptBody):
    model_id: str = Field(max_length=100)
    efforts: list[str] = Field(max_length=10)


class Claim(ScriptBody):
    worker_id: str = Field(pattern=ID)
    models: list[Capability] = Field(max_length=20)


class Start(ScriptBody):
    attempt_id: str = Field(pattern=ID)


class Complete(Start):
    status: str = Field(pattern="^(succeeded|failed|uncertain)$")
    text: str | None = Field(default=None, max_length=256 * 1024)
    sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    error: str | None = Field(default=None, max_length=2000)
