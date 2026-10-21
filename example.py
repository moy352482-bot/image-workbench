"""运行已有本地图片的完整登记流程，不生成图片，也不修改源文件。"""

import argparse
from pathlib import Path

from image_workbench import StorageConfig, StorageManager


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reference", type=Path, required=True, help="已有参考图片")
    parser.add_argument("--output-v1", type=Path, required=True, help="已有第一版结果图片")
    parser.add_argument("--output-v2", type=Path, required=True, help="已有修改后的第二版图片")
    parser.add_argument("--data-root", type=Path, help="可选数据目录，未指定时使用配置默认值")
    args = parser.parse_args()

    # 先检查三个素材存在，减少输错路径时产生空项目的情况。
    for source in (args.reference, args.output_v1, args.output_v2):
        if not source.is_file():
            parser.error(f"图片源文件不存在或不是普通文件: {source}")

    storage = StorageManager(StorageConfig(data_root=args.data_root))
    project = storage.create_project("我的图片项目")
    task = storage.create_task(project.project_id, "generate", "参考上传图片，绘制森林小屋")
    reference = storage.add_input_image(project.project_id, task.task_id, args.reference)
    version1 = storage.register_output_image(project.project_id, task.task_id, args.output_v1)

    print("项目:", project.project_id)
    print("参考图:", reference.artifact_id)
    print("第一版:", storage.get_artifact_path(version1.artifact_id))

    edit_task = storage.create_task(
        project.project_id, "edit", "将天空调整为晚霞",
        base_artifact_id=version1.artifact_id,
    )
    version2 = storage.register_output_image(project.project_id, edit_task.task_id, args.output_v2)
    print("第二版:", storage.get_artifact_path(version2.artifact_id))
    print("第二版来源:", version2.source_artifact_id)

    # 从新版本回溯到旧版本，两张图片都保留在各自任务中。
    current = version2
    while True:
        print("历史:", current.artifact_id, storage.get_artifact_path(current.artifact_id))
        if current.source_artifact_id is None:
            break
        current = storage.get_artifact(current.source_artifact_id)


if __name__ == "__main__":
    main()
