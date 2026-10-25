"""消费权威发送记录；消息先提交后发送，结果不明时不盲目重发机器人消息。"""
import logging
from app.infrastructure.assistant_queue import AssistantQueueError
from app.infrastructure.dingtalk_robot import RobotUnavailable
from app.model.assistant_queue import PublishAssistantTask
from app.model.question_tree import utc_now

logger=logging.getLogger(__name__)


class AssistantDeliveryService:
    def __init__(self,repository,queue,robot,*,publication_retry_window_seconds=3600):
        self.repository,self.queue,self.robot=repository,queue,robot
        self.publication_retry_window_seconds=publication_retry_window_seconds

    async def flush(self,task_id):
        for item in await self.repository.deliveries(task_id):
            if not await self.deliver(item):return False
        return True

    async def deliver(self,item,*,retry_after_seconds=None):
        if item.status=='sent':return True
        if item.error_code=='delivery_unknown':
            if item.destination=='user':return False
            # 重试窗口必须短于中间件幂等回执保留期，超过窗口的未知结果只保留待核对。
            if (utc_now()-item.created_at).total_seconds()>self.publication_retry_window_seconds:return False
        attempt_options={} if retry_after_seconds is None else {"retry_after_seconds":retry_after_seconds}
        if not await self.repository.delivery_attempt(item,**attempt_options):return False
        try:
            if item.destination=='user':
                response=await self.robot.send_markdown(item.recipient_userid,'助理回复',item.message)
                receipt={'processQueryKey':response.processQueryKey}
            else:
                response=await self.queue.publish(PublishAssistantTask(userid=item.recipient_userid,
                    task_type=item.task_type,message=item.message,request_id=item.request_id,
                    source_assistant_id=item.userid if item.task_type=='assistant_message' else None))
                receipt={'task_id':response.task.task_id,'duplicate':response.duplicate}
            await self.repository.delivery_result(item,receipt=receipt)
        except (RobotUnavailable,AssistantQueueError) as error:
            # 明确未成功可稍后重试。超时/取消/回执未能持久化保留预先写入的 unknown。
            if not error.uncertain:
                await self.repository.delivery_result(item,retryable=True)
            logger.warning('助理消息交付未完成：destination=%s error_type=%s uncertain=%s',
                           item.destination,type(error).__name__,error.uncertain)
            return False
        except Exception as error:
            logger.warning('助理消息交付未确认：error_type=%s',type(error).__name__)
            return False
        return True
