# Office 统一中文字体

字体：Noto Sans CJK SC，Regular与Bold，来源notofonts/noto-cjk官方仓库：

- https://github.com/notofonts/noto-cjk/tree/main/Sans/OTF/SimplifiedChinese
- 许可证：随包OFL.txt（SIL Open Font License 1.1）。

仅作用于Office转换临时副本，不覆盖源文件、不修改图片像素或已有PDF。字体随Docker的assets目录打包；转换器通过临时fontconfig显式加载本目录，不依赖本机绝对路径或用户字体安装。正文、东亚及复杂文字统一家族，字号、粗体属性等保留；Noto未覆盖的符号可能仍使用系统后备字体。
