"""Application composition root: create services, then attach HTTP adapters.

This is the only place that assembles all website features. Route installers
receive their dependencies and must not construct unrelated feature services.
"""
import os

from .accounts_web import AccountApplication
from .admin_web import install_admin
from .storage import StorageManager
from .script_service import ScriptService
from .script_web import install_scripts
from .video_store import VideoStore
from .video_web import install_video
from .video_pipeline import install_pipeline
from .pipeline_service import PipelineService
from .storyboard_service import StoryboardService
from .storyboard_web import install_storyboards
from .plugin_api import PluginRegistry
from .script_plugin import PLUGIN_ID, ScriptGenerationPlugin
from .script_workflow import ScriptStoryboardWorkflow
from .deepseek_text import DeepSeekTextAdapter
from .script_api_models import DeepSeekModelRegistry
from .storyboard_planner import StoryboardPlanner
from .prompt_enhancement import PromptEnhancementService
from .http_api.prompt_enhancements import install_routes as install_prompt_enhancements


def create_app(storage: StorageManager | None = None, *, backend="codex", remote_access=None, registration_policy=None,
               disabled_plugins=None):
    disabled = set(disabled_plugins if disabled_plugins is not None else
                   filter(None, (item.strip() for item in os.environ.get("IMAGE_WORKBENCH_DISABLED_PLUGINS", "").split(","))))
    if disabled - {PLUGIN_ID}:
        raise ValueError(f"Unknown disabled plugins: {sorted(disabled - {PLUGIN_ID})}")
    application = AccountApplication(storage or StorageManager(), backend=backend,
                                     remote_access=remote_access, registration_policy=registration_policy)
    application.video_store = VideoStore(application.storage, application.identity, enabled_backends=("online-2.0",))
    registry = DeepSeekModelRegistry()
    text_adapter = DeepSeekTextAdapter()
    application.script_service = ScriptService(application.storage, application.identity,
                                               registry=registry, adapter=text_adapter)
    # Prompt enhancement is stateless: it never receives storage, identity or
    # the image backend. HTTP resolves the current service on every request so
    # tests can replace it without rebuilding routes.
    application.prompt_enhancement_service = PromptEnhancementService.from_config()
    # Resolve text service per operation so tests/integrations can replace the
    # service without rebuilding routes. Identity and workspace scope stay intact.
    def scripts():
        return application.script_service

    def workflow():
        return application.script_workflow

    def images(aid, wid):
        return application.scoped_app(aid, wid).state.workbench

    application.plugins = PluginRegistry(services=("script-service",))
    application.plugins.register(ScriptGenerationPlugin(workflow, application.plugins), enabled=PLUGIN_ID not in disabled)
    application.pipeline_service = PipelineService(application.video_store, scripts, images)
    install_admin(application)
    install_video(application.auth, application.video_store, application.pipeline_service)
    install_scripts(application.auth, scripts, application.storage, remote_mode=bool(remote_access),
                    get_workflow=workflow,
                    get_plugin=lambda: application.plugins.get(PLUGIN_ID),
                    describe_plugin=lambda: application.plugins.describe(PLUGIN_ID))
    if remote_access:
        from .script_remote import ScriptRemoteQueue, RemoteScriptRegistry
        from .http_api.script_workers import install_script_worker
        application.script_queue = ScriptRemoteQueue(application.script_service)
        registry = RemoteScriptRegistry(application.script_queue, config_root=registry.root)
        application.script_service.registry = registry
        application.script_service.router.registry = registry
        install_script_worker(application.auth, application.script_queue, after_script=lambda task: workflow().after_script(task))
    application.storyboard_service = StoryboardService(application.storage, application.identity,
                                                       lambda aid, wid, vid: scripts().storyboard_source(aid, wid, vid),
                                                       application.video_store, registry=DeepSeekModelRegistry(),
                                                       planner=StoryboardPlanner(adapter=text_adapter),
                                                       pricing_path=registry.root / "storyboard_pricing.json")
    application.script_workflow = ScriptStoryboardWorkflow(scripts, lambda: application.storyboard_service)
    install_storyboards(application.auth, application.storyboard_service, resume_pending=application.script_workflow.resume_pending)
    install_pipeline(application.auth, application.pipeline_service, application.video_store)
    install_prompt_enhancements(application.auth, lambda: application.prompt_enhancement_service)
    return application
