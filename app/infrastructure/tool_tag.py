"""显式加载模型工具格式；每个 Agent 持有独立快照，导入不读盘。"""
from app.config.mllm import MLLMSettings


def tool_tag_section(template: str, heading: str = '工具调用格式：') -> str:
    """关闭时正文与标题同时省略；保留模板内的 JSON 花括号和标签原文。"""
    if not isinstance(template,str) or (template and not template.strip()):
        raise ValueError('工具格式模板必须是有效字符串，禁用时使用空字符串')
    return f'{heading}\n{template}' if template else ''


def resolve_mllm_tool_tag(settings: MLLMSettings, override: str | None = None) -> str:
    """在 Agent 装配时加载，失败不回退；显式关闭优先于宿主传入的模板。"""
    if not settings.tool_tag_enabled:
        return ''
    if override is not None:
        tool_tag_section(override)
        return override
    path=settings.tool_tag_path
    try:
        # 不接受符号链接，包括目录内链接；静态资产不能转向其他文件。
        if path.is_symlink() or path.resolve().parent != path.parent.resolve():
            raise ValueError('工具格式模板不能使用符号链接或越出模板目录')
        content=path.read_text(encoding='utf-8')
    except (OSError,UnicodeError,RuntimeError):
        raise ValueError('无法读取工具格式模板，请检查静态资产与编码') from None
    if not content.strip():
        raise ValueError('工具格式模板不能为空')
    return content.removesuffix('\n')
