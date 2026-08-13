---
name: pdf-report
description: 生成 PDF 文档（.pdf）。适用于对外分发的正式报告、存档留证类交付物。输入为结构化 JSON 规格（与 docx-report 同构），输出可下载的 pdf 文件。
---

# PDF 报告生成

## 步骤

1. 查询业务数据后，用 write_file 写规格到 `/work/pdf_spec.json`（与 docx-report 完全同构：title/subtitle/sections[heading/paragraphs/bullets/table]）。数据必须来自真实工具查询结果。

2. 执行：

```
python skills/doc-skills/pdf-report/scripts/make_pdf.py work/pdf_spec.json work/核查报告.pdf
```

3. 脚本打印 `OK <路径>`，把文件路径告知用户。中文字体已由脚本自动处理，无需关心。

## 硬性纪律

- 产物一律写到 `/work/` 目录（execute 命令里写 `work/` 相对路径也等价），**禁止写 /tmp 或任何工作区之外的路径**；
- 脚本执行失败时如实报告 stderr，**禁止内联重写生成代码绕过脚本**——脚本是审计与格式一致性的保证。
