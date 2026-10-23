"""Worker request schemas."""
from pydantic import BaseModel, ConfigDict, Field
ID = r"^[0-9a-f]{32}$"

class Claim(BaseModel):
    model_config = ConfigDict(extra="forbid")
    worker_id: str = Field(pattern=ID)
    backend: str = Field(pattern=r"^(codex|local|image-api)$")


class Complete(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: str = Field(pattern=r"^(succeeded|failed|uncertain)$")
    hashes: list[str] = Field(default_factory=list, max_length=32)
    failure_type: str | None = None


class Start(BaseModel):
    model_config = ConfigDict(extra="forbid")
    attempt_id: str = Field(pattern=ID)
