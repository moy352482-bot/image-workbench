"""Shared MP4 envelope validation; no storage, identity or worker dependencies."""
import struct

MAX_OUTPUT_BYTES = 40 * 1024 * 1024

def inspect_mp4(data):
    """检查 ISO BMFF 盒边界和 MP4 标识；真实编码检查由 H3 执行器负责。"""
    if not isinstance(data, bytes) or not 24 <= len(data) <= MAX_OUTPUT_BYTES:
        raise ValueError("视频大小无效或超过40 MiB")
    offset, boxes = 0, {}
    while offset < len(data):
        if len(data) - offset < 8:
            raise ValueError("MP4盒头不完整")
        size, kind = struct.unpack_from(">I4s", data, offset)
        header = 8
        if size == 1:
            if len(data) - offset < 16:
                raise ValueError("MP4扩展盒头不完整")
            size = struct.unpack_from(">Q", data, offset + 8)[0]
            header = 16
        elif size == 0:
            size = len(data) - offset
        if size < header or size > len(data) - offset:
            raise ValueError("MP4盒长度无效")
        boxes.setdefault(kind, []).append(data[offset + header:offset + size])
        offset += size
    ftyp = boxes.get(b"ftyp", [b""])[0]
    brands = {ftyp[:4], *(ftyp[i:i + 4] for i in range(8, len(ftyp), 4))}
    if len(ftyp) < 8 or not brands.intersection({b"isom", b"iso2", b"iso4", b"iso5", b"iso6", b"mp41", b"mp42", b"avc1"}):
        raise ValueError("只接受MP4视频")
    if not any(boxes.get(b"moov", [])) or not any(boxes.get(b"mdat", [])):
        raise ValueError("MP4缺少视频元数据或媒体数据")


