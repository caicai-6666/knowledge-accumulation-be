# 附件分类转换为 PDF

个人助理钉钉入站下载完成后，按格式调用独立转换器；所有成功附件继续使用既有 PDF 暂存、队列、UUID 和上传元信息流程。探索 HTTP 上传也复用这些转换器，引用消息类型范围不因本次扩展而改变。

## 模块与接口

入口为 [robot_attachment.py](../../../app/infrastructure/robot_attachment.py) 的 `supported`、`to_pdf`。文件扩展名用于路由，实际字节必须通过对应格式校验，不能把改名文件当作文档。转换器不下载、不写入上传元信息，不创建任务。

| 模块 | 输入与行为 |
| --- | --- |
| [image.py](../../../app/infrastructure/file_conversion/image.py) | PNG、JPEG、WebP、BMP、GIF、TIFF、AVIF、HEIC/HEIF；完整解码、修正方向、透明铺白、无损 PNG 嵌入 PDF，多帧逐页保留。 |
| [word.py](../../../app/infrastructure/file_conversion/word.py) | DOC/DOCX，以 OLE 流或 OOXML 类型声明验证；经 LibreOffice Writer 导出。 |
| [excel.py](../../../app/infrastructure/file_conversion/excel.py) | XLS/XLSX，以 OLE 流或 OOXML 类型声明验证；经 Calc 整表导出，每表一页，包含隐藏表、忽略打印范围，不压缩到 A4。 |
| [powerpoint.py](../../../app/infrastructure/file_conversion/powerpoint.py) | 个人助理及探索上传支持PPT/PPTX；识别OLE演示流或OOXML类型，经Impress导出，一张幻灯片一页，包含隐藏页，不导出备注页。 |
| [pdf.py](../../../app/infrastructure/file_conversion/pdf.py) | 已有 PDF 校验密码、页面及格式后原样返回，不重编码。 |

转换器成功返回 `ConvertedPDF(content, source_format, page_count)`；失败使用 `ConversionError(code, reason)`。Word/Excel 的格式识别、共享 Office 引擎及字体处理迁自 `project-management-be/app/tool/file_conversion/`，没有引入对兄弟项目的运行时依赖。图片复用其解码和无损封装思路，同时保留本项目多帧、AVIF 与正常页面一像素一点的行为。

## Office 引擎与字体

[office.py](../../../app/infrastructure/file_conversion/office.py) 每份文档使用独立临时目录与 LibreOffice profile，禁用宏执行及外部链接自动更新。先转 ODT/ODS 临时副本，再用 [fonts.py](../../../app/infrastructure/file_conversion/fonts.py) 统一 Noto Sans CJK SC，最后导出 PDF；原件不回写。统一字体可能改变换行与版式，不承诺与 Microsoft Office 完全一致。公式、图表等结果取决于 LibreOffice 的兼容性。

`OFFICE_CONVERTER_EXECUTABLE` 可指定转换程序；留空自动查找 `soffice`、`libreoffice` 或标准 macOS 应用路径。Dockerfile 安装 Writer、Calc、Impress 及后备字体，复制 `assets/fonts/` 的常规/粗体字体、来源说明及 OFL 许可证。Python 依赖新增 `olefile==0.47`，无需修改数据库或 deploy 仓库。部署需重建本项目镜像。

转换期间每进程最多两份 Office 文档；等待槽位最多 60 秒，拿到槽位后两次导出共用另一个 60 秒期限。不是端到端 60 秒承诺：文件准备、字体处理和 PDF 验证另需时间。导出超时终止整个进程组并等待退出。正常和异常退出均删除临时原件、配置及中间文件；强制杀死服务进程仍可能遗留系统临时目录。

转换及 Core 文件阅读共用 [pdf_lock.py](../../../app/infrastructure/pdf_lock.py) 的可重入锁，避免 MuPDF 跨线程同时操作。

## 容量和失败边界

- Office 输入、临时 ODF 与输出上限各为 32 MiB；压缩包最多 10000 项、解压声明总量不超过 256 MiB。个人助理支持 DOC/DOCX、XLS/XLSX、PPT/PPTX，不支持 DOCM/XLSM/PPTM。探索上传同样支持PPT/PPTX，仍受单份10MiB、合计20MiB限制。
- 图片沿用机器人单文件上限（默认 50 MiB），每帧默认 4000 万像素、100 帧；累计像素不超过单帧上限的四倍。转换后还需满足队列和本地存储上限。高位深/浮点图像明确拒绝；HEIF 解码为显示像素，不承诺 HDR 增益图保留。
- 转换引擎、字体缺失或转换槽位忙，保留入站记录等待重推，不伪装为坏文件。损坏、加密、格式不支持、容量超限或转换超时，按既有整条拒绝通知说明原因。
- 不采用对方的“失败保留原件并继续”策略；本项目只允许已验证的 PDF 入队，不将 Word/Excel/PowerPoint 原件伪装为 PDF。

## 取消、重试与存储

转换成功只保存 PDF，不额外长期保存原始 Office 文件或图片。Office 输出即使清理时间戳和文档 ID，也不保证重复转换逐字节一致。

入站按消息和附件位置分配稳定 UUID。优先读取已暂存 PDF；存在时不重新下载、转换或覆盖。转换与保存处于同一取消保护区：取消不会立即终止工作线程；超时和服务关闭可能重复取消接收任务，每次等待都通过 shield 隔离子任务，宿主等待有界转换及可靠保存完成才传播取消并释放消息锁。回调超时后仍返回未完成状态，下一次重推复用已保存结果；不会因为每次转换超过回调期限而永远无法暂存。队列发布、最终 UUID、上传元信息、历史引用及原有附件清理规则不变。

## 验证

`tests/test_attachment_converters.py` 使用合成 DOCX/XLSX、真实 LibreOffice 生成的旧 DOC/XLS，以及内存图片，检查中文文本、页数、表格末端内容、格式拒绝、超时进程清理和临时目录清理。样本位于 `tests/fixtures/attachment-conversion/`，临时 Office 工作目录位于 `tests/tmp/`。

`tests/test_robot_office_conversion.py` 检查 PDF 发布、重推复用、缺少引擎时保留接收状态，以及取消后先可靠保存再退出。现有机器人回归继续覆盖 HEIC 多帧、消息顺序、引用复用和拒绝通知。此类入站测试使用网络替身，没有向真实用户发送消息；未验证容器镜像构建或真实钉钉 Word/Excel 收发。

2026-11-13 本地验证：转换器 6 项全部通过（包含真实 LibreOffice 的 DOC/DOCX、XLS/XLSX）；机器人回归 79 项中 75 项通过，4 项需要隔离 MySQL 环境的测试跳过。`pip check` 与 `git diff --check` 通过。未修改其他项目、实际业务数据或发送真实消息。

独立验收发现并修复重复取消会穿透等待的边界；新增两次取消及 Stream 超时后关闭服务测试，均确认保存完成后才退出、未发布半条任务。

project-management-be 独立复验通过：连续三次取消均未提前结束，释放转换后保存一次并传播取消；未取消时原始异常正常传播。独立重跑机器人回归 80 项，76 项通过、4 项实库跳过；结合前轮真实 Office 转换 6 项及图片授权 7 项通过，本次代码与本地测试范围验收通过。未覆盖 Docker 构建、真实钉钉收发或强杀进程/机器崩溃。


---

## 探索上传接入

[exploration_attachment.py](../../../app/service/exploration_attachment.py) 将带文件名的原始上传转换为既有 `UploadedFile(type=images)`。任务服务先校验树归属，再在线程池顺序转换整批附件；全部成功才登记待激活任务，转换失败不取消被替代的旧任务。原始字节仅用于转换，不长期保存；后续 UUID、文件补偿和任务引用沿用既有流程，不写个人助理 uploaded_files。

原始文件和输出 PDF 均限制单份 10 MiB、合计 20 MiB，最多 10 份。Office 复用引擎、字体、并发及超时规则；取消请求不登记任务，已经运行的转换线程由转换器正常结束或超时并清理临时目录。HTTP 状态、支持扩展名及前端交互约定见[探索接口](../../api/exploration.md)。

`tests/test_exploration_upload_conversion.py` 验证真实 PNG/DOCX/XLSX 混合转换、PDF 原样保留、归属校验优先、转换失败不替代旧任务、输出限额及实际 HTTP 注册/错误分类。探索相关测试 12 项通过、9 项隔离 MySQL 测试未启用；尚未进行前后端联调。测试不访问业务数据库，不写真实数据目录。


---

## PowerPoint 接入（2026-11-18）

个人助理和探索上传均已接入PPT/PPTX路由及拒绝提示，探索multipart上传沿用原有任务注册和SSE激活方式。PPTX检查标准演示主文档类型、必要条目、ZIP大小与重复路径，拒绝宏包与加密包；旧PPT检查 `PowerPoint Document`、`Current User` 流，加密标记明确拒绝。识别后仍需LibreOffice完整导入并验证PDF，不能仅凭包头保证文件有效。

共用Office流程为PowerPoint选择ODP中间格式，沿用中文字体归一、进程隔离、并发/超时、PDF验证与临时目录清理。Impress导出包含隐藏幻灯片，不包含备注页；保留幻灯片页面大小，图片不主动降采样。动画、音视频与交互不作为可播放内容保留；字体替换、复杂图形和Office兼容性可能改变细节，不承诺像素级还原。

Dockerfile新增 `libreoffice-impress`。本机完整LibreOffice安装可直接使用；部署需要包含Impress的新镜像，旧的仅Writer/Calc依赖层不能据此假定支持PPT。无需新增Python依赖、环境变量、数据库迁移，本次未修改deploy仓库或部署运行服务。

`tests/test_powerpoint_conversion.py` 用合成演示经真实LibreOffice生成PPT/PPTX，验证中文、隐藏页、16:9尺寸、路由、宏格式/伪装文件拒绝、加密识别、大小限制和依赖失败分类。用户提供的27页PPTX已在本地转换并渲染检查，证据见[本地实验记录](../../../experiment/powerpoint-conversion/20261118-user-sample/analysis.md)；原文件不改写，输出仅在experiment中，不进入真实上传目录或发送给员工。

探索接入补充验证：`tests/test_powerpoint_conversion.py` 使用真实合成PPT/PPTX经HTTP multipart上传，确认返回201、附件为有效PDF且任务仍待SSE激活；探索测试覆盖损坏/宏类型拒绝、缺引擎503、转换输出超限413以及失败不登记任务。前端项目未在本次直接修改，文件选择器和客户端校验需由前端同步放开。
