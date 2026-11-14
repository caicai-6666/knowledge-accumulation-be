"""在临时ODF中统一字体；保留命名空间前缀、公式、字号、粗细及表格布局。"""
from io import BytesIO
from pathlib import Path
from zipfile import ZipFile
from lxml import etree
from app.config.middleware import PROJECT_ROOT
from .model import ConversionError

FONT_FAMILY = 'Noto Sans CJK SC'
FONT_DIRECTORY = PROJECT_ROOT / 'assets' / 'fonts'
NS = {'office':'urn:oasis:names:tc:opendocument:xmlns:office:1.0',
      'style':'urn:oasis:names:tc:opendocument:xmlns:style:1.0',
      'svg':'urn:oasis:names:tc:opendocument:xmlns:svg-compatible:1.0',
      'fo':'urn:oasis:names:tc:opendocument:xmlns:xsl-fo-compatible:1.0'}


def require_fonts():
    for name in ('NotoSansCJKsc-Regular.otf','NotoSansCJKsc-Bold.otf'):
        if not (FONT_DIRECTORY/name).is_file():
            raise ConversionError('font_unavailable','缺少统一中文字体资产，请重新安装或构建镜像。')


def normalize_odf_fonts(content: bytes) -> bytes:
    """同时覆盖正文、默认样式、富文本及嵌入图表；不修改原始Office字节。"""
    output=BytesIO()
    parser=etree.XMLParser(resolve_entities=False,no_network=True,remove_blank_text=False)
    with ZipFile(BytesIO(content)) as source,ZipFile(output,'w') as target:
        if sum(i.file_size for i in source.infolist())>256*1024*1024:
            raise ConversionError('document_too_large','临时Office文档过大。')
        for item in source.infolist():
            data=source.read(item)
            if item.filename.endswith(('content.xml','styles.xml')):
                root=etree.fromstring(data,parser)
                # lxml保留of:公式等属性值引用的原命名空间，不用标准库重写丢失前缀。
                declarations=root.find('office:font-face-decls',NS)
                if declarations is None:
                    declarations=etree.Element('{'+NS['office']+'}font-face-decls')
                    root.insert(0,declarations)
                if not any(face.get('{'+NS['style']+'}name')==FONT_FAMILY for face in declarations):
                    face=etree.SubElement(declarations,'{'+NS['style']+'}font-face')
                    face.set('{'+NS['style']+'}name',FONT_FAMILY)
                    face.set('{'+NS['svg']+'}font-family',FONT_FAMILY)
                for element in root.iter():
                    if element.tag=='{'+NS['style']+'}font-face':
                        element.set('{'+NS['svg']+'}font-family',FONT_FAMILY)
                    if element.tag=='{'+NS['style']+'}text-properties':
                        for name in ('font-name','font-name-asian','font-name-complex'):
                            element.set('{'+NS['style']+'}'+name,FONT_FAMILY)
                    if '{'+NS['fo']+'}font-family' in element.attrib:
                        element.set('{'+NS['fo']+'}font-family',FONT_FAMILY)
                data=etree.tostring(root,xml_declaration=True,encoding='UTF-8')
            target.writestr(item,data)
    return output.getvalue()
