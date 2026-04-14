# VBA 模板库 — 莫兰迪配色组会 PPT

> Claude 根据此模板库 + JSON outline 生成完整 VBA 代码

## 模块结构

生成的 VBA 模块包含以下部分：
1. 颜色、字体、图片路径常量（含 `IMG_BASE`）
2. 工具函数（AddFormattedTextbox、AddStandardFrame、AddImage 等）
3. 8 个幻灯片创建 Sub（每种类型一个，含 TwoColumnImage 图片页）
4. 主入口 `GeneratePresentation()`

---

## 1. 常量定义

```vba
Option Explicit

' ===== 莫兰迪色系 =====
' 实际代码中直接使用 RGB() 函数（更可读），这里仅供参考
Const MORANDI_BLUE As Long = 11706767    ' RGB(143, 161, 178)
Const MORANDI_DARK As Long = 6444875     ' RGB(75, 85, 98)
Const MORANDI_GREEN As Long = 9809059    ' RGB(163, 172, 149)
Const MORANDI_ROSE As Long = 9345461     ' RGB(181, 151, 142)
Const MORANDI_CREAM As Long = 14410987   ' RGB(235, 228, 219)
Const MORANDI_CHARCOAL As Long = 4079166 ' RGB(62, 62, 62)
Const MORANDI_WHITE As Long = 16120058   ' RGB(250, 248, 245)
Const VBA_WHITE As Long = 16777215       ' RGB(255, 255, 255)

' ===== 字体 =====
Const FONT_CN As String = "Microsoft YaHei"
Const FONT_EN As String = "Arial"

' ===== 字号 (Points) =====
Const TITLE_SIZE As Long = 28
Const SUBTITLE_SIZE As Long = 24
Const BODY_SIZE As Long = 20
Const SMALL_SIZE As Long = 16
Const CAPTION_SIZE As Long = 14
Const PAGE_NUM_SIZE As Long = 12

' ===== 幻灯片尺寸 (Points, 16:9) =====
Const SLIDE_WIDTH As Long = 960
Const SLIDE_HEIGHT As Long = 540

' ===== 图片路径基目录 =====
' Claude 根据项目实际路径生成（绝对 Windows 路径，反斜杠结尾）
Const IMG_BASE As String = "C:\Users\xxx\project\data\"  ' <-- Claude 替换
```

**IMG_BASE**：根据阶段三图片映射表中所有图片路径的最长公共前缀计算，必须以 `\` 结尾。

---

## 2. 工具函数

### AddFormattedTextbox

```vba
Function AddFormattedTextbox(sld As Slide, sText As String, _
    Left As Single, Top As Single, Width As Single, Height As Single, _
    FontSize As Long, Bold As Boolean, FontColor As Long, _
    Optional Alignment As Long = 1, _
    Optional FontName As String = "Arial") As Shape

    Dim shp As Shape
    Set shp = sld.Shapes.AddTextbox(msoTextOrientationHorizontal, _
        Left, Top, Width, Height)

    With shp.TextFrame
        .WordWrap = msoTrue
        .AutoSize = ppAutoSizeShapeToFitText
        With .TextRange
            .Text = sText
            .Font.Size = FontSize
            .Font.Bold = Bold
            .Font.Color.RGB = FontColor
            .Font.Name = FontName
            .ParagraphFormat.Alignment = Alignment
        End With
    End With

    Set AddFormattedTextbox = shp
End Function
```

### AddSpeakerNotes

```vba
Sub AddSpeakerNotes(sld As Slide, notesText As String)
    If Len(notesText) > 0 Then
        sld.NotesPage.Shapes.Placeholders(2) _
            .TextFrame.TextRange.Text = notesText
    End If
End Sub
```

### AddColoredShape

```vba
Function AddColoredShape(sld As Slide, shapeType As Long, _
    Left As Single, Top As Single, Width As Single, Height As Single, _
    FillColor As Long) As Shape

    Dim shp As Shape
    Set shp = sld.Shapes.AddShape(shapeType, Left, Top, Width, Height)
    shp.Fill.Solid
    shp.Fill.ForeColor.RGB = FillColor
    shp.Line.Visible = msoFalse

    Set AddColoredShape = shp
End Function
```

### AddStandardFrame（标准内容页框架）

5 种内容页（Content / TwoColumn / TwoColumnImage / Table / Conclusion）共享此框架。

```vba
Sub AddStandardFrame(sld As Slide, sTitle As String, pageNum As Long)
    ' 顶部标题栏（MorandiDark，白色文字对比度 ~7.7:1）
    AddColoredShape sld, msoShapeRectangle, 0, 0, SLIDE_WIDTH, 86, RGB(75, 85, 98)
    ' 蓝色装饰线（MorandiBlue，仅装饰）
    AddColoredShape sld, msoShapeRectangle, 0, 86, SLIDE_WIDTH, 6, RGB(143, 161, 178)
    ' 标题文字
    AddFormattedTextbox sld, sTitle, 36, 25, 720, 58, _
        TITLE_SIZE, True, RGB(255, 255, 255), ppAlignLeft, FONT_CN
    ' 页码
    AddFormattedTextbox sld, CStr(pageNum), 864, 504, 72, 25, _
        PAGE_NUM_SIZE, False, RGB(143, 161, 178), ppAlignRight, FONT_EN
End Sub
```

### AddBulletLines

```vba
Sub AddBulletLines(sld As Slide, contentLines() As String, _
    Left As Single, Top As Single, Width As Single, Height As Single, _
    FontSize As Long, FontColor As Long)

    Dim shp As Shape
    Set shp = sld.Shapes.AddTextbox(msoTextOrientationHorizontal, _
        Left, Top, Width, Height)

    With shp.TextFrame
        .WordWrap = msoTrue
        .AutoSize = ppAutoSizeShapeToFitText

        Dim i As Long
        For i = LBound(contentLines) To UBound(contentLines)
            Dim rng As TextRange
            If i = LBound(contentLines) Then
                Set rng = .TextRange
            Else
                Set rng = .TextRange.InsertAfter(vbCrLf)
            End If

            Dim bulletRng As TextRange
            Set bulletRng = .TextRange.InsertAfter(Chr(8226) & " " & contentLines(i))
            bulletRng.Font.Size = FontSize
            bulletRng.Font.Color.RGB = FontColor
            bulletRng.Font.Name = FONT_CN
        Next i
    End With
End Sub
```

### ApplyHighlight

```vba
Sub ApplyHighlight(tr As TextRange, keyword As String, HighlightColor As Long)
    Dim pos As Long
    pos = InStr(1, tr.Text, keyword, vbTextCompare)
    Do While pos > 0
        Dim hlRng As TextRange
        Set hlRng = tr.Characters(pos, Len(keyword))
        hlRng.Font.Bold = msoTrue
        hlRng.Font.Color.RGB = HighlightColor
        pos = InStr(pos + Len(keyword), tr.Text, keyword, vbTextCompare)
    Loop
End Sub
```

### AddImage（图片插入 + 占位符回退）

```vba
Function AddImage(sld As Slide, imgPath As String, _
    Left As Single, Top As Single, Width As Single, Height As Single, _
    Optional caption As String = "") As Shape
    ' 文件存在则插入图片（等比缩放居中），不存在则绘制 Cream 占位符

    Dim shp As Shape

    If Len(Dir(imgPath)) > 0 Then
        ' --- 插入图片（原始尺寸，保持长宽比） ---
        Set shp = sld.Shapes.AddPicture( _
            FileName:=imgPath, LinkToFile:=msoFalse, _
            SaveWithDocument:=msoTrue, _
            Left:=0, Top:=0, Width:=-1, Height:=-1)
        shp.LockAspectRatio = msoTrue

        ' 等比缩放以适应目标区域（Fit 模式，不裁剪不拉伸）
        Dim scaleW As Single, scaleH As Single
        scaleW = Width / shp.Width
        scaleH = Height / shp.Height
        If scaleW < scaleH Then
            shp.Width = Width
        Else
            shp.Height = Height
        End If
        ' 居中放置于目标区域
        shp.Left = Left + (Width - shp.Width) / 2
        shp.Top = Top + (Height - shp.Height) / 2
    Else
        ' --- 占位符：Cream + 虚线边框 + 文件名提示 ---
        Set shp = sld.Shapes.AddShape(msoShapeRectangle, Left, Top, Width, Height)
        shp.Fill.Solid
        shp.Fill.ForeColor.RGB = RGB(235, 228, 219)
        shp.Line.Visible = msoTrue
        shp.Line.ForeColor.RGB = RGB(200, 193, 184)
        shp.Line.DashStyle = msoLineDash

        Dim fileName As String
        Dim lastSlash As Long
        lastSlash = InStrRev(imgPath, "\")
        If lastSlash > 0 Then fileName = Mid(imgPath, lastSlash + 1) Else fileName = imgPath

        Dim lblShp As Shape
        Set lblShp = sld.Shapes.AddTextbox(msoTextOrientationHorizontal, _
            Left + 10, Top + Height / 2 - 20, Width - 20, 40)
        With lblShp.TextFrame.TextRange
            .Text = "[Image not found: " & fileName & "]"
            .Font.Size = 12
            .Font.Color.RGB = RGB(143, 161, 178)
            .Font.Name = "Arial"
            .Font.Italic = msoTrue
            .ParagraphFormat.Alignment = ppAlignCenter
        End With
    End If

    If Len(caption) > 0 Then
        Dim capShp As Shape
        Set capShp = sld.Shapes.AddTextbox(msoTextOrientationHorizontal, _
            Left, Top + Height + 4, Width, 24)
        With capShp.TextFrame.TextRange
            .Text = caption
            .Font.Size = 10
            .Font.Color.RGB = RGB(143, 161, 178)
            .Font.Name = "Arial"
            .Font.Italic = msoTrue
            .ParagraphFormat.Alignment = ppAlignCenter
        End With
    End If

    Set AddImage = shp
End Function
```

---

## 3. 幻灯片类型模板

### CreateTitleSlide（封面页 — 独立布局）

```vba
Sub CreateTitleSlide(prs As Presentation, sTitle As String, _
    sSubtitle As String, sPresenter As String, sDate As String, _
    Optional sNotes As String = "")

    Dim sld As Slide
    Set sld = prs.Slides.Add(prs.Slides.Count + 1, ppLayoutBlank)

    ' 左侧蓝色条
    AddColoredShape sld, msoShapeRectangle, 0, 0, 29, SLIDE_HEIGHT, RGB(143, 161, 178)
    ' 底部绿色装饰条
    AddColoredShape sld, msoShapeRectangle, 29, 490, SLIDE_WIDTH - 29, 50, RGB(163, 172, 149)
    ' 标题
    AddFormattedTextbox sld, sTitle, 72, 144, 792, 108, _
        40, True, RGB(75, 85, 98), ppAlignLeft, FONT_CN
    ' 副标题
    If Len(sSubtitle) > 0 Then
        AddFormattedTextbox sld, sSubtitle, 72, 252, 792, 58, _
            SUBTITLE_SIZE, False, RGB(62, 62, 62), ppAlignLeft, FONT_CN
    End If
    ' 汇报人 + 日期
    AddFormattedTextbox sld, sPresenter, 72, 396, 432, 36, _
        18, False, RGB(62, 62, 62), ppAlignLeft, FONT_CN
    AddFormattedTextbox sld, sDate, 72, 432, 432, 36, _
        SMALL_SIZE, False, RGB(143, 161, 178), ppAlignLeft, FONT_EN

    AddSpeakerNotes sld, sNotes
End Sub
```

### CreateSectionSlide（章节分隔页 — 独立布局）

```vba
Sub CreateSectionSlide(prs As Presentation, sectionNum As Long, _
    sectionTitle As String, Optional sNotes As String = "")

    Dim sld As Slide
    Set sld = prs.Slides.Add(prs.Slides.Count + 1, ppLayoutBlank)

    ' 左半 MorandiDark 背景
    AddColoredShape sld, msoShapeRectangle, 0, 0, 468, SLIDE_HEIGHT, RGB(75, 85, 98)
    ' MorandiBlue 分隔线
    AddColoredShape sld, msoShapeRectangle, 454, 0, 28, SLIDE_HEIGHT, RGB(143, 161, 178)
    ' 编号（绿色）
    AddFormattedTextbox sld, Format(sectionNum, "00"), _
        58, 158, 144, 58, 48, True, RGB(163, 172, 149), ppAlignLeft, FONT_EN
    ' 标题（白色）
    AddFormattedTextbox sld, sectionTitle, _
        58, 216, 360, 86, 36, True, RGB(255, 255, 255), ppAlignLeft, FONT_CN

    AddSpeakerNotes sld, sNotes
End Sub
```

### CreateContentSlide（内容页 — 标准框架）

```vba
Sub CreateContentSlide(prs As Presentation, sTitle As String, _
    contentLines() As String, pageNum As Long, _
    Optional sNotes As String = "")

    Dim sld As Slide
    Set sld = prs.Slides.Add(prs.Slides.Count + 1, ppLayoutBlank)
    AddStandardFrame sld, sTitle, pageNum

    ' 全宽 bullet 列表
    AddBulletLines sld, contentLines, 36, 115, 864, 396, BODY_SIZE, RGB(62, 62, 62)

    AddSpeakerNotes sld, sNotes
End Sub
```

### CreateTwoColumnSlide（双栏页 — 标准框架）

```vba
Sub CreateTwoColumnSlide(prs As Presentation, sTitle As String, _
    leftContent() As String, rightPlaceholder As String, _
    pageNum As Long, Optional sNotes As String = "")

    Dim sld As Slide
    Set sld = prs.Slides.Add(prs.Slides.Count + 1, ppLayoutBlank)
    AddStandardFrame sld, sTitle, pageNum

    ' 左栏 bullet
    AddBulletLines sld, leftContent, 36, 115, 418, 360, 18, RGB(62, 62, 62)
    ' 右栏 Cream 占位区
    AddColoredShape sld, msoShapeRectangle, 490, 130, 432, 324, RGB(235, 228, 219)
    AddFormattedTextbox sld, rightPlaceholder, 540, 270, 332, 43, _
        SMALL_SIZE, False, RGB(143, 161, 178), ppAlignCenter, FONT_CN

    AddSpeakerNotes sld, sNotes
End Sub
```

### CreateTwoColumnImageSlide（双栏图片页 — 标准框架）

> 左侧 bullet + 右侧图片。阶段三映射了图片的幻灯片使用此模板。

```vba
Sub CreateTwoColumnImageSlide(prs As Presentation, sTitle As String, _
    leftContent() As String, imgPath As String, _
    pageNum As Long, _
    Optional imgCaption As String = "", _
    Optional sNotes As String = "")

    Dim sld As Slide
    Set sld = prs.Slides.Add(prs.Slides.Count + 1, ppLayoutBlank)
    AddStandardFrame sld, sTitle, pageNum

    ' 左栏 bullet
    AddBulletLines sld, leftContent, 36, 115, 418, 370, 18, RGB(62, 62, 62)
    ' 右栏图片（AddImage 自动处理存在/缺失）
    AddImage sld, imgPath, 490, 115, 432, 370, imgCaption

    AddSpeakerNotes sld, sNotes
End Sub
```

**布局坐标**：左栏 (36, 115, 418, 370) | 右栏图片 (490, 115, 432, 370)

### CreateTableSlide（表格页 — 标准框架）

```vba
Sub CreateTableSlide(prs As Presentation, sTitle As String, _
    headers() As String, dataRows() As String, _
    numDataRows As Long, numCols As Long, pageNum As Long, _
    Optional sNotes As String = "")
    ' dataRows: 一维数组，行优先排列

    Dim sld As Slide
    Set sld = prs.Slides.Add(prs.Slides.Count + 1, ppLayoutBlank)
    AddStandardFrame sld, sTitle, pageNum

    ' 原生表格
    Dim totalRows As Long
    totalRows = numDataRows + 1
    Dim tbl As Table
    Set tbl = sld.Shapes.AddTable(totalRows, numCols, 58, 130, 828, 43 * totalRows).Table

    ' 表头: MorandiDark 背景 + 白色粗体
    Dim c As Long
    For c = 1 To numCols
        With tbl.Cell(1, c)
            .Shape.TextFrame.TextRange.Text = headers(c - 1)
            .Shape.TextFrame.TextRange.Font.Size = CAPTION_SIZE
            .Shape.TextFrame.TextRange.Font.Bold = msoTrue
            .Shape.TextFrame.TextRange.Font.Color.RGB = RGB(255, 255, 255)
            .Shape.TextFrame.TextRange.Font.Name = FONT_CN
            .Shape.Fill.ForeColor.RGB = RGB(75, 85, 98)
        End With
    Next c

    ' 数据行: 交替 White/Cream 背景
    Dim r As Long
    For r = 1 To numDataRows
        For c = 1 To numCols
            Dim idx As Long
            idx = (r - 1) * numCols + (c - 1)
            With tbl.Cell(r + 1, c)
                .Shape.TextFrame.TextRange.Text = dataRows(idx)
                .Shape.TextFrame.TextRange.Font.Size = 12
                .Shape.TextFrame.TextRange.Font.Color.RGB = RGB(62, 62, 62)
                .Shape.TextFrame.TextRange.Font.Name = FONT_CN
                If r Mod 2 = 0 Then
                    .Shape.Fill.ForeColor.RGB = RGB(235, 228, 219)
                Else
                    .Shape.Fill.ForeColor.RGB = RGB(250, 248, 245)
                End If
            End With
        Next c
    Next r

    AddSpeakerNotes sld, sNotes
End Sub
```

### CreateConclusionSlide（结论页 — 标准框架）

```vba
Sub CreateConclusionSlide(prs As Presentation, sTitle As String, _
    findings() As String, nextSteps() As String, _
    hasNextSteps As Boolean, pageNum As Long, _
    Optional sNotes As String = "")

    Dim sld As Slide
    Set sld = prs.Slides.Add(prs.Slides.Count + 1, ppLayoutBlank)
    AddStandardFrame sld, sTitle, pageNum

    ' 主要发现（绿色圆点 + 文字）
    Dim yPos As Single: yPos = 130
    Dim i As Long
    For i = LBound(findings) To UBound(findings)
        AddColoredShape sld, msoShapeOval, 43, yPos + 7, 18, 18, RGB(163, 172, 149)
        AddFormattedTextbox sld, findings(i), 79, yPos, 792, 58, _
            18, False, RGB(62, 62, 62), ppAlignLeft, FONT_CN
        yPos = yPos + 65
    Next i

    ' 下一步计划
    If hasNextSteps Then
        yPos = yPos + 22
        AddFormattedTextbox sld, "Next Steps:", 36, yPos, 288, 36, _
            18, True, RGB(143, 161, 178), ppAlignLeft, FONT_EN
        yPos = yPos + 36
        AddBulletLines sld, nextSteps, 36, yPos, 864, 108, SMALL_SIZE, RGB(62, 62, 62)
    End If

    AddSpeakerNotes sld, sNotes
End Sub
```

### CreateThankYouSlide（致谢页 — 独立布局）

```vba
Sub CreateThankYouSlide(prs As Presentation, _
    Optional sEmail As String = "", _
    Optional sNotes As String = "")

    Dim sld As Slide
    Set sld = prs.Slides.Add(prs.Slides.Count + 1, ppLayoutBlank)

    ' 全深色背景（MorandiDark）
    AddColoredShape sld, msoShapeRectangle, 0, 0, SLIDE_WIDTH, SLIDE_HEIGHT, RGB(75, 85, 98)
    ' 底部蓝色装饰条（MorandiBlue）
    AddColoredShape sld, msoShapeRectangle, 0, 468, SLIDE_WIDTH, 72, RGB(143, 161, 178)

    AddFormattedTextbox sld, "Thank You", 0, 180, SLIDE_WIDTH, 108, _
        56, True, RGB(255, 255, 255), ppAlignCenter, FONT_EN
    AddFormattedTextbox sld, "Questions & Discussion", 0, 288, SLIDE_WIDTH, 58, _
        SUBTITLE_SIZE, False, RGB(235, 228, 219), ppAlignCenter, FONT_EN

    If Len(sEmail) > 0 Then
        AddFormattedTextbox sld, sEmail, 0, 396, SLIDE_WIDTH, 36, _
            SMALL_SIZE, False, RGB(255, 255, 255), ppAlignCenter, FONT_EN
    End If

    AddSpeakerNotes sld, sNotes
End Sub
```

---

## 4. 主入口模板

```vba
Sub GeneratePresentation()
    Dim prs As Presentation
    Set prs = Application.Presentations.Add
    prs.PageSetup.SlideWidth = 960
    prs.PageSetup.SlideHeight = 540

    ' ===== 以下由 Claude 根据 JSON outline 填充 =====
    ' CreateTitleSlide prs, "标题", "副标题", "汇报人", "日期", "讲稿"
    '
    ' Dim c1() As String
    ' c1 = Split("要点1|要点2|要点3", "|")
    ' CreateContentSlide prs, "AE断言式标题", c1, 2, "讲稿"
    '
    ' Dim c2() As String
    ' c2 = Split("Finding 1|Finding 2", "|")
    ' CreateTwoColumnImageSlide prs, "AE标题", c2, _
    '     IMG_BASE & "fig.png", 5, "Fig caption", "讲稿"

    MsgBox "PPT generated! " & prs.Slides.Count & " slides.", vbInformation
End Sub
```

---

## 5. Claude 生成规则

1. **直接使用 `RGB()` 函数**，不使用预计算 Long 常量
2. **字符串中的中文**：VBA 字符串直接支持 Unicode，无需转义
3. **数组构造**：`Split("item1|item2|item3", "|")`
4. **高亮关键词**：内容生成后调用 `ApplyHighlight` 处理
5. **斜体处理**：细菌名/基因名用 `Characters().Font.Italic = msoTrue`
6. **讲稿换行**：`vbCrLf` 替换 `\n`
7. **完整性**：生成代码包含所有工具函数 + 所有幻灯片调用 + 主入口，粘贴即运行
8. **IMG_BASE**：值为阶段三确认的图片公共路径前缀（绝对 Windows 路径，`\` 结尾）
9. **图片页**：映射了图片的幻灯片用 `CreateTwoColumnImageSlide`，其余用 `CreateContentSlide`
10. **路径拼接**：统一用 `IMG_BASE & "subdir\filename.png"`
11. **AddImage 必须包含**：即使只有一张图片也需要（确保占位符回退）
