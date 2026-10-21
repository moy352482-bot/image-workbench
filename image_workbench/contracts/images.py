"""Image workspace request contracts; no routes or business services."""
from pydantic import BaseModel, ConfigDict, Field, StrictBool

ID_PATTERN = r"^[0-9a-f]{32}$"
MAX_UPLOAD_BYTES = 40 * 1024 * 1024
MAX_AUTH_BYTES = 16 * 1024

class Payload(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CreateProject(Payload):
    request_id: str = Field(pattern=ID_PATTERN)
    name: str = Field(min_length=1, max_length=120)
    description: str = Field(default="", max_length=4000)


class ProjectUpdate(Payload):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    description: str | None = Field(default=None, max_length=4000)
    status: str | None = None


class TaskUpdate(Payload):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    description: str | None = Field(default=None, max_length=4000)


class MoveTask(Payload):
    target_subproject_id: str = Field(pattern=ID_PATTERN)
    expected_subproject_id: str | None = Field(default=None, pattern=ID_PATTERN)


class RunTask(Payload):
    request_id: str = Field(pattern=ID_PATTERN)
    confirm_generation: StrictBool = False
    retry_of: str | None = Field(default=None, pattern=ID_PATTERN)


class RestoreTask(Payload):
    request_id: str = Field(pattern=ID_PATTERN)
    execution_id: str = Field(pattern=ID_PATTERN)
