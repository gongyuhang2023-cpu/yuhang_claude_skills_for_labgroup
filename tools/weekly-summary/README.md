# Weekly Summary — 每周工作总结生成器

从 Windows Sticky Notes 便笺中自动读取本周工作记录，调用 DeepSeek V4 Pro 生成结构化中英文双语周报。

## 功能

- 自动读取 Windows Sticky Notes 中的活跃便笺（14 天内有更新）
- 按项目分组，逐条任务 + 完成状态
- 忠实于便笺原文，用户写了小结就转述，没写就不加
- 中英文双语 Markdown 周报，每周追加（不覆盖历史）
- "启动下一周"：一键在便笺中写入下周日期头
- Deep Navy 深色主题 GUI，高 DPI 支持

## 安装方式

### 方式一：安装包（推荐）

从 [Releases](https://github.com/gongyuhang2023-cpu/yuhang_claude_skills_for_labgroup/releases) 下载 `WeeklySummary_Setup.exe`，双击安装。

### 方式二：从源码运行

```bash
pip install -r requirements.txt
python weekly_summary_gui.py
```

### 方式三：自行打包

```bash
# 需要 Python 3.10+
pip install pyinstaller
python build.bat

# 如需安装包，安装 NSIS 3.x 后 build.bat 会自动生成
```

## 首次使用

1. 启动后自动弹出设置窗口
2. 填写中文姓名、英文姓名
3. 填写 DeepSeek API Key（[申请地址](https://platform.deepseek.com/api_keys)）
4. 点击"测试连接"确认 Key 有效
5. 选择周报保存位置（默认桌面）
6. 保存，回到主界面点击"生成本周总结"

## 便笺书写约定

每个项目对应一个 Sticky Note，格式如下：

```
项目名称 MM-DD ~ MM-DD
1. 任务描述（✅）
   经验或说明（可选，AI 会忠实转述）
2. 另一个任务（⏳进行中）
3. 未完成的任务（❌原因说明）
```

- `✅` / `完成` / `done` → 已完成
- `⏳` / `进行中` → 进行中
- `❌` / `⚠️` / `未完成` → 未完成

## 配置文件

配置保存在 `%APPDATA%\WeeklySummary\config.json`，卸载不会删除。

## 技术栈

- Python + customtkinter（GUI）
- DeepSeek V4 Pro API（OpenAI SDK 兼容）
- Windows Sticky Notes SQLite 数据库直读
- PyInstaller + NSIS 打包
