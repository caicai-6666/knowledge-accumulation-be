"""纯 token 计数校验与 3:7 配额计算，不读取存储或发起模型请求。"""

def validate_count(value):
    if type(value) is not int or value<0:
        raise ValueError('分词计数必须为非负整数')
    return value


def budget(context_window,output_tokens,management_tokens,fixed_tokens):
    dynamic=context_window-output_tokens-management_tokens-fixed_tokens
    if dynamic<=3:
        raise ValueError('固定输入与输出预留已耗尽上下文')
    workspace=dynamic*3//10
    return workspace,dynamic-workspace
