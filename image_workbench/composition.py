"""Application composition root: create services, then attach HTTP adapters.

This is the only place that assembles all website features. Route installers
receive their dependencies and must not construct unrelated feature services.
"""
from .accounts_web import AccountApplication
from .admin_web import install_admin
from .storage import StorageManager
from .script_service import ScriptService
from .script_web import install_scripts
from .video_store import VideoStore
from .video_web import install_video
from .video_pipeline import install_pipeline
from .pipeline_service import PipelineService
from .qwen_lab import QwenLabStore
from .qwen_web import install_qwen
from .storyboard_service import StoryboardService
from .storyboard_web import install_storyboards


def create_app(storage: StorageManager | None = None, *, backend="codex", remote_access=None, registration_policy=None):
    application = AccountApplication(storage or StorageManager(), backend=backend,
                                     remote_access=remote_access, registration_policy=registration_policy)
    application.video_store = VideoStore(application.storage, application.identity)
    application.script_service = ScriptService(application.storage, application.identity)
    application.qwen_store = QwenLabStore(application.storage, application.identity)
    # Resolve text service per operation so tests/integrations can replace the
    # service without rebuilding routes. Identity and workspace scope stay intact.
    scripts = lambda: application.script_service
    images = lambda aid, wid: application.scoped_app(aid, wid).state.workbench
    application.pipeline_service = PipelineService(application.video_store, scripts, images)
    install_admin(application)
    install_video(application.auth, application.video_store)
    install_scripts(application.auth, scripts, application.storage, remote_mode=bool(remote_access))
    if remote_access:
        from .script_remote import ScriptRemoteQueue, RemoteScriptRegistry
        from .http_api.script_workers import install_script_worker
        application.script_queue = ScriptRemoteQueue(application.script_service)
        registry = RemoteScriptRegistry(application.script_queue)
        application.script_service.registry = registry
        application.script_service.router.registry = registry
        install_script_worker(application.auth, application.script_queue)
    application.storyboard_service = StoryboardService(application.storage, application.identity,
                                                       application.script_service, application.video_store)
    application.script_service.storyboards = application.storyboard_service
    install_storyboards(application.auth, application.storyboard_service)
    install_pipeline(application.auth, application.pipeline_service, application.video_store)
    install_qwen(application.auth, application.qwen_store)
    return application
