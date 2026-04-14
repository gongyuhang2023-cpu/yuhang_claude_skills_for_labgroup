# 科研 PPT 文字规范

> 确保学术准确性

## 斜体规则

### 必须斜体

| 类型 | 格式 | 正确示例 | 错误示例 |
|------|------|----------|----------|
| 细菌属种名 | *斜体* | *E. coli*, *S. aureus* | E. coli, S. aureus |
| 基因名 | *斜体* | *lacZ*, *VEGFA* | lacZ, VEGFA |
| 拉丁术语 | *斜体* | *in vitro*, *in vivo* | in vitro, in vivo |

### 不用斜体

| 类型 | 格式 | 正确示例 |
|------|------|----------|
| 蛋白名 | 正体 | LacZ, VEGFA |
| 噬菌体常用名 | 正体 | T4, T7, λ phage |
| 细菌复数/泛指 | 正体小写 | staphylococci, salmonellae |

### 基因命名规范

| 物种 | 基因 | 蛋白 | 示例 |
|------|------|------|------|
| 人类 | *全大写斜体* | 全大写正体 | *BRCA1* → BRCA1 |
| 小鼠 | *首字母大写斜体* | 全大写正体 | *Brca1* → BRCA1 |
| 细菌 | *三小写+大写斜体* | 首字母大写正体 | *lacZ* → LacZ |

---

## 常见细菌名对照

### 革兰氏阴性菌

| 全名 | 缩写 | PPT 中写法 |
|------|------|------------|
| *Escherichia coli* | *E. coli* | 斜体 |
| *Pseudomonas aeruginosa* | *P. aeruginosa* | 斜体 |
| *Klebsiella pneumoniae* | *K. pneumoniae* | 斜体 |
| *Acinetobacter baumannii* | *A. baumannii* | 斜体 |

### 革兰氏阳性菌

| 全名 | 缩写 | PPT 中写法 |
|------|------|------------|
| *Staphylococcus aureus* | *S. aureus* | 斜体 |
| *Streptococcus pneumoniae* | *S. pneumoniae* | 斜体 |
| *Enterococcus faecalis* | *E. faecalis* | 斜体 |

---

## 数字与单位

### 格式要求

| 规则 | 正确 | 错误 |
|------|------|------|
| 数字与单位间空格 | 10 μL | 10μL |
| 科学计数法 | 1.5 × 10⁸ | 1.5*10^8 |
| 温度 | 37°C | 37 ℃ 或 37度 |
| 百分比 | 95% | 95 % |

### 常用单位

| 类型 | 标准写法 |
|------|----------|
| 微升 | μL (不是 ul) |
| 毫升 | mL (不是 ml) |
| 纳米 | nm |
| 转速 | rpm |
| 滴度 | PFU/mL |

---

## 统计术语

### P 值

| 正确写法 | 说明 |
|----------|------|
| *P* < 0.05 | P 斜体 |
| *P* = 0.032 | 具体值 |
| **P* < 0.05 | 显著性标记 |

### 其他

| 术语 | 写法 |
|------|------|
| 标准差 | SD 或 s.d. |
| 标准误 | SEM 或 s.e.m. |
| 样本量 | *n* = 5（n 斜体） |

---

## VBA 中的斜体实现

### PowerPoint 快捷键

- Windows: `Ctrl + I`

### VBA 代码

```vba
' 设置斜体
Dim rng As TextRange
Set rng = tf.TextRange.Characters(startPos, length)
rng.Font.Italic = msoTrue
```

### 自动检测关键词

以下词汇应自动应用斜体：

```
E. coli, S. aureus, P. aeruginosa, K. pneumoniae,
A. baumannii, S. pneumoniae, E. faecalis, B. subtilis,
in vitro, in vivo, in situ, et al.
```

---

## 自查清单

生成 PPT 前检查：

- [ ] 所有细菌名是否斜体？
- [ ] 基因名和蛋白名格式是否正确？
- [ ] 数字与单位之间是否有空格？
- [ ] P 值是否斜体且格式正确？
- [ ] 科学计数法是否使用 × 而非 *？
