"""沿用参考项目的自适应代码围栏，正文不能提前关闭所属资料区域。"""
import re
import yaml


def text_block(text,*,language='text'):
    fence='`'*max(3,1+max((len(run) for run in re.findall(r'`+',text)),default=0))
    return f'{fence}{language}\n{text}\n{fence}'


def metadata_block(value):
    return text_block(yaml.safe_dump(value,allow_unicode=True,sort_keys=False).rstrip('\n'),language='yaml')
