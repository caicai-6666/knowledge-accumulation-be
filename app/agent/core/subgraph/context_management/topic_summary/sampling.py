"""主题规划与总结共用参考项目的独立采样；推理强度来自宿主配置。"""


def summary_generation(configured):
    """保留配置的推理强度、输出额度和 seed，不修改主任务的生成配置。"""
    return configured.model_copy(update={
        'enable_thinking': True,
        'temperature': 1.0,
        'top_p': 0.95,
        'top_k': 20,
        'min_p': 0.0,
        'presence_penalty': 0.0,
        'repetition_penalty': 1.0,
    })
