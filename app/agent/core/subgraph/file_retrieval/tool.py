"""内部检索工具只有引用和计数反馈；没有列表查看入口。"""
from dataclasses import replace
from textwrap import dedent
from pydantic import ValidationError
from ...json_codec import load_model_json, validate_model_payload, ModelActionRejected, model_validation_feedback
from ...tool.registry import RegisteredTool, ToolRegistry
from ...tool.result import OrdinaryToolContent, ToolExecutionResult
from .schema import NameQuery, SummaryQuery, PassportQuery, UnionResults, FinishRetrieval

MODELS = {'search_files_by_name':NameQuery,'search_files_by_summary':SummaryQuery,
          'search_files_by_passport':PassportQuery,
          'union_file_results':UnionResults,'finish_file_retrieval':FinishRetrieval}


def definitions():
    return [{'type':'function','function':{'name':name,'description':model.__doc__,
        'parameters':model.model_json_schema(),'strict':False}} for name,model in MODELS.items()]


async def execute(pool, search, name, raw):
    try:
        model=MODELS.get(name)
        if model is None:
            raise ModelActionRejected('请求的工具不在当前文件检索工具列表中，未执行查询。', '请选择本轮实际提供的工具。')
        args=validate_model_payload(model,load_model_json(raw) if isinstance(raw,str) else raw)
        if name=='finish_file_retrieval':
            ranking=pool.get(args.result_id) if args.result_id is not None else None
            return {'status':'success','finished':True,'count':len(ranking.hits) if ranking else 0},ranking,args.explanation
        if name=='union_file_results':
            rid=pool.union(args.left_result_id,args.right_result_id)
        else:
            scope=pool.scope(args.result_id)
            field = {'search_files_by_name':'name', 'search_files_by_summary':'abstract',
                     'search_files_by_passport':'passport'}[name]
            try:
                hits=await search(field,args.passport if field=='passport' else args.query,scope)
            except ModelActionRejected:
                raise
            except Exception:
                return {'status':'error','fatal':True,
                    'message':'文件查询未能完成，本次未取得可用结果，不能解释为没有相关文件。',
                    'required_action':'请稍后重新检索；不能根据本次失败判断文件是否存在。'},None,None
            if not hits:
                # 查询期间父引用可能已释放；空结果也必须核对引用有效性。
                pool.scope(args.result_id)
                return {'status':'success','count':0},None,None
            rid=pool.put(hits,field if field!='abstract' else 'summary',args.result_id,exact=field=='passport')
        return {'status':'success','result_id':rid,'count':len(pool.get(rid).hits)},None,None
    except ValidationError as error:
        feedback = model_validation_feedback(error, model)
        return {'status':'error','message':feedback.reason,'required_action':feedback.required_action},None,None
    except ModelActionRejected as error:
        return {'status':'error','message':error.reason,'required_action':error.required_action},None,None
    except (ValueError,TypeError):
        return {'status':'error','fatal':True,
            'message':'文件候选结果未通过处理校验，本次未完成检索。',
            'required_action':'请重新检索；不要把未通过校验的候选当作有效结果。'},None,None


def render_file_retrieval_feedback(arguments, execution, context):
    feedback = execution.result.tool_result
    if execution.result.status == 'failed':
        text = dedent("""
            ### 文件检索工具

            状态：操作未完成
            原因：{message}
            下一步：{required_action}
        """).strip().format(**feedback)
    elif feedback.get('finished'):
        # 完成工具成功后直接结束子循环；不声称此回执被下一轮模型读取。
        text = dedent("""
            ### 文件检索

            状态：候选选择已完成。
        """).strip()
    elif feedback['count'] == 0:
        text = dedent("""
            ### 文件候选检索

            状态：查询完成，当前条件下未找到候选。
            候选文件：0 个
            未生成候选结果集 ID；如需继续，请调整查询条件。
        """).strip()
    else:
        text = dedent("""
            ### 文件候选集合

            状态：候选集合已生成
            候选结果集 ID：{result_id}
            候选文件：{count} 个
            下一步：可在此集合内继续查询、合并集合，或用 finish_file_retrieval 提交。
        """).strip().format(**feedback)
    return replace(
        execution,
        result=execution.result.model_copy(update={'content': OrdinaryToolContent(text=text)}),
    )


class FileRetrievalToolSession:
    """内部终态排名只留在本次会话，不序列化给模型或混入反馈正文。"""

    def __init__(self, pool, search):
        self.pool = pool
        self.search = search
        self.result = None
        self.registry = ToolRegistry([
            RegisteredTool(
                name,
                model.__doc__,
                model,
                lambda name, raw, workspace, arguments: workspace,
                executor=self.executor_for(name),
                hooks=(render_file_retrieval_feedback,),
            )
            for name, model in MODELS.items()
        ])

    def executor_for(self, name):
        async def run(arguments):
            feedback, ranking, explanation = await execute(
                self.pool, self.search, name, arguments.model_dump(mode='json'),
            )
            if feedback.get('finished'):
                self.result = (ranking, explanation)
            return ToolExecutionResult(
                status='failed' if feedback['status'] == 'error' else 'succeeded',
                tool_result=feedback,
            )
        return run
