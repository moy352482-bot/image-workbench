import sys

from .cli import main


if __name__ == "__main__":
    # 管道和 PowerShell 均得到 UTF-8 JSON；不依赖当前 Windows 控制台代码页。
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    raise SystemExit(main())
