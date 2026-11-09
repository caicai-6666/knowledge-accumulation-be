"""钉钉日程读取契约；保留额外业务字段，供后续结果查看使用。"""
from datetime import date, datetime

from pydantic import BaseModel, ConfigDict, Field, model_validator


class CalendarReadError(RuntimeError):
    """只传递产生错误的分支提供的安全反馈，不携带上游异常原文。"""
    def __init__(self, code: str, message: str):
        super().__init__(code)
        self.code, self.message = code, message


class CalendarTime(BaseModel):
    model_config = ConfigDict(extra='allow', frozen=True, hide_input_in_errors=True)
    date: str | None = Field(default=None, strict=True)
    dateTime: str | None = Field(default=None, strict=True)
    timeZone: str | None = Field(default=None, strict=True)

    @model_validator(mode='after')
    def valid_time(self):
        if self.dateTime:
            value = datetime.fromisoformat(self.dateTime.replace('Z', '+00:00'))
            if value.tzinfo is None or value.utcoffset() is None:
                raise ValueError('日程时间缺少时区')
        elif self.date:
            date.fromisoformat(self.date)
        else:
            raise ValueError('日程时间缺失')
        return self


class CalendarEvent(BaseModel):
    model_config = ConfigDict(extra='allow', frozen=True, hide_input_in_errors=True)
    id: str = Field(strict=True, min_length=1)
    start: CalendarTime
    end: CalendarTime
    summary: str | None = Field(default=None, strict=True)
    description: str | None = Field(default=None, strict=True)
    attendees: list[dict] | None = None


class CalendarEventPage(BaseModel):
    model_config = ConfigDict(extra='ignore', hide_input_in_errors=True)
    events: list[CalendarEvent] = Field(max_length=100)
    nextToken: str | None = Field(default=None, strict=True)
