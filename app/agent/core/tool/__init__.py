"""主助手工具公共入口；按能力拆分定义，通过注册表统一组合。"""
from .registry import RegisteredTool,ToolRegistry,build_registry,build_builtin_registrations,check_descriptions
from .base import FinishTaskTool,OutputArguments
from .context import CompactArguments,build_context_registrations
from .image_viewer import ViewImageFileArguments,build_image_view_registration
from .external_expert import (AskExternalExpertArguments,ContinueExternalExpertArguments,ExternalExpertSessionPool,
    build_external_expert_registrations,build_external_expert_tools,render_external_expert_reply)
from .web_search import (SearchWebArguments,ViewWebSearchResultsArguments,WebSearchPool,WebSearchService,build_web_search_registrations)
from .web_page import OpenWebPageArguments,WebPageViewer,build_open_web_page_registration
from .progress import ToolProgress
from .hooks import ToolHook,ToolHookContext
from .result import (ToolExecution,ToolExecutionResult,ToolGuidance,ToolPageReference,
    OrdinaryToolContent,FoldableToolContent,ToolContent,ToolContentType)
from .workspace import (ExplorationResult,WorkspaceAddArguments,WorkspaceReplaceArguments,WorkspaceDeleteArguments,
    WorkspaceEdit,WorkspacePath,prepare_workspace_edit,parse_workspace_tool_arguments,build_workspace_registrations)

__all__=['SearchWebArguments','ViewWebSearchResultsArguments','WebSearchPool','WebSearchService','build_web_search_registrations',
    'OpenWebPageArguments','WebPageViewer','build_open_web_page_registration','ToolProgress','AskExternalExpertArguments','ContinueExternalExpertArguments','ExternalExpertSessionPool',
    'ViewRegistryFileArguments','build_registry_file_registration','build_file_search_registration','FileCandidateViewer','build_file_candidates_registration',
    'build_external_expert_registrations','build_external_expert_tools','render_external_expert_reply','RegisteredTool','ToolRegistry','build_registry','check_descriptions','OutputArguments','CompactArguments',
    'ExplorationResult','WorkspaceAddArguments','WorkspaceReplaceArguments','WorkspaceDeleteArguments',
    'WorkspaceEdit','WorkspacePath','prepare_workspace_edit','parse_workspace_tool_arguments',
    'build_workspace_registrations','FinishTaskTool','build_builtin_registrations','build_context_registrations',
    'ViewImageFileArguments','build_image_view_registration','ToolExecution','ToolExecutionResult','ToolGuidance','ToolPageReference',
    'OrdinaryToolContent','FoldableToolContent','ToolContent','ToolContentType']

from .date_calculator import DateCalculateArguments,build_date_calculation_registration
from .current_time import GetCurrentTimeArguments,build_current_time_registration

__all__ += ["DateCalculateArguments","build_date_calculation_registration",
            "GetCurrentTimeArguments","build_current_time_registration"]

from .sub_management_platform import SubManagementPlatformViewer,build_sub_management_platform_registration

__all__ += ["SubManagementPlatformViewer","build_sub_management_platform_registration"]

from .file_search import build_file_search_registration
from .file_candidates import FileCandidateViewer, build_file_candidates_registration

from .registry_file import ViewRegistryFileArguments, build_registry_file_registration

__all__ += ['ToolHook', 'ToolHookContext']
