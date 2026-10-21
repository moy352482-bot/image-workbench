"""Resolve logical image references without creating extra stored artifacts."""
from .storage import _validate_id


def task_input_artifacts(storage, project_id, task_id):
    inputs = [item for item in storage.list_task_artifacts(project_id, task_id) if item.kind == "input"]
    try:
        options = storage._read_json(storage._path(".workbench", "tasks", project_id, task_id + ".json"))
    except FileNotFoundError:
        return inputs
    if (options.get("project_id"), options.get("task_id")) != (project_id, task_id):
        raise ValueError("任务素材引用记录无效")
    ids = options.get("reference_artifact_ids", [])
    if not isinstance(ids, list) or len(ids) > 8 or len(ids) != len(set(ids)):
        raise ValueError("任务素材引用列表无效")
    known = {item.artifact_id for item in inputs}
    for aid in ids:
        artifact = storage.get_artifact(_validate_id(aid))
        if artifact.project_id != project_id or artifact.format not in {"PNG", "JPEG", "WEBP"}:
            raise ValueError("任务引用了不属于当前项目的图片")
        if aid not in known:
            inputs.append(artifact)
            known.add(aid)
    if len(inputs) > 8:
        raise ValueError("最多引用 8 张图片")
    return inputs
