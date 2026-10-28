"""Server-side text model catalog; independent of a local Codex installation."""
from .deepseek_text import MODEL, EFFORTS, api_key
from .script_models import CONFIG_ROOT, ScriptModelRegistry


class DeepSeekModelRegistry(ScriptModelRegistry):
    def __init__(self, *, available=None):
        super().__init__(CONFIG_ROOT / "deepseek")
        self.available = available

    def models(self):
        ready = bool(api_key()) if self.available is None else self.available
        catalog = {"models": [{"slug": MODEL, "supported_reasoning_levels": list(EFFORTS)}] if ready else []}
        values = ScriptModelRegistry(self.root, catalog=catalog).models()
        for value in values:
            value["availability_source"] = "deepseek_api_configuration"
            if not value["available"]:
                value["unavailable_reason"] = "DeepSeek API 尚未配置"
        return values
