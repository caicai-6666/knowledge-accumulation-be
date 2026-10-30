"""业务路由与跨后端调用共用的应用路径；直接开发入口设置 /dev。"""
import os

APPLICATION_PREFIX = f'{os.environ.get("APP_PATH_PREFIX", "")}/knowledge-accumulation'
API_PREFIX = f'{APPLICATION_PREFIX}/api'
