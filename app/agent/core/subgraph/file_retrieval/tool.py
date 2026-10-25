"""内部检索工具只有引用和计数反馈；没有列表查看入口。"""
from pydantic import ValidationError
from ...json_codec import load_model_json, validate_model_payload
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
            raise ValueError('请选择当前提供的工具。')
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
            except Exception:
                return {'status':'error','fatal':True,'message':'文件查询依赖暂不可用或资料已变化，本次检索失败，不能解释为无结果。'},None,None
            if not hits:
                # 查询期间父引用可能已释放；空结果也必须核对引用有效性。
                pool.scope(args.result_id)
                return {'status':'success','count':0},None,None
            rid=pool.put(hits,field if field!='abstract' else 'summary',args.result_id,exact=field=='passport')
        return {'status':'success','result_id':rid,'count':len(pool.get(rid).hits)},None,None
    except ValidationError as error:
        fields=','.join('.'.join(map(str,item['loc'])) for item in error.errors(include_input=False))
        return {'status':'error','message':'参数字段不合法：'+fields},None,None
    except (ValueError,TypeError):
        return {'status':'error','message':'工具名称、参数或结果引用不合法；请按当前Schema修正，失效集合需重新查询。'},None,None
