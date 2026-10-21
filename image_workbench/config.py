"""存储配置；默认目录不受启动脚本的工作目录影响。"""

from dataclasses import dataclass
import os
from pathlib import Path


@dataclass(frozen=True)
class StorageConfig:
    data_root: Path | str | None = None
    max_image_bytes: int = 20 * 1024 * 1024
    max_image_pixels: int = 40_000_000
    max_text_bytes: int = 1024 * 1024

    def __post_init__(self) -> None:
        for name in ("max_image_bytes", "max_image_pixels", "max_text_bytes"):
            value = getattr(self, name)
            if type(value) is not int or value <= 0:
                raise ValueError(f"{name} 必须是正整数")
        root = self.data_root
        if root is None:
            root = os.environ.get("IMAGE_WORKBENCH_DATA_ROOT")
        if root is None:
            root = Path(__file__).resolve().parent.parent / "data"
        if not isinstance(root, (str, Path)) or not str(root).strip():
            raise ValueError("data_root 必须是非空目录路径")
        object.__setattr__(self, "data_root", Path(root).expanduser().resolve())
