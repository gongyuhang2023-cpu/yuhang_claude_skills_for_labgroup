# -*- coding: utf-8 -*-
"""
AI周报总结器
使用DeepSeek V4 Pro对便笺内容进行智能总结，支持中英文双语输出
"""

import os
from datetime import datetime
from pathlib import Path

SUMMARY_PROMPT_CN = """你是一位专业的科研工作助手。请根据以下便笺记录，生成本周工作总结。

## 输出格式

按项目分组，逐条列出任务：

### 项目名称
- ✅/⏳/❌ 任务描述
  小结：（仅当用户在便笺中写了说明时，忠实简要地转述；用户没写则不加小结）

### 【材料短缺提醒】（如有）
- 需要补充的材料列表

## 核心规则
1. **只总结最近一周的内容**。便笺中可能有历史记录，请只关注最新一周日期范围内的任务
2. 从便笺中识别不同项目（通常每个便笺对应一个项目），按项目分组
3. 根据标记判断完成状态：✅/"完成"/"done" → 已完成；⏳/"进行中" → 进行中；❌/⚠️/"未完成" → 未完成
4. **小结必须忠实于用户原文**：用户在任务后写了经验、原因、说明，就简要转述；用户没写，就不要加小结。绝对不可以自行编造建议、分析原因或添加改进方案
5. 技能训练（德语、R语言、运动等）合并为一个"日常事务"板块，简洁列出即可
6. 如便笺中提到材料短缺、耗材不足，添加【材料短缺提醒】
7. 语言简洁专业，不要写"下周计划"、"关键进展与成果"等额外板块，除非用户便笺中有明确内容
8. 中文Markdown格式输出

便笺记录内容：
---
{notes_content}
---

请生成本周工作总结："""

SUMMARY_PROMPT_EN = """You are a professional research work assistant. Generate this week's work summary from the notes below.

## Output Format

Group by project, list each task:

### Project Name
- ✅/⏳/❌ Task description
  Note: (only if the user wrote an explanation in their notes — faithfully paraphrase it; if the user wrote nothing, omit this line entirely)

### Material Shortage Alert (if applicable)
- Items to restock

## Rules
1. **Only summarize the most recent week.** Notes may contain historical entries — focus exclusively on tasks within the latest week's date range
2. Group tasks by project (each sticky note typically = one project)
3. Status markers: ✅/"完成"/"done" → Completed; ⏳/"进行中" → In Progress; ❌/⚠️/"未完成" → Incomplete
4. **Notes must be faithful to user's own words**: if the user wrote an explanation, experience, or reason after a task, paraphrase it briefly; if the user wrote nothing, do NOT invent advice, analyze reasons, or add improvement suggestions
5. Combine skill training (German, R, exercise, etc.) into one "Daily Routines" section, keep it brief
6. If notes mention material shortages, add a Material Shortage Alert section
7. Keep it concise and professional. Do NOT add extra sections like "Next Week Plan" or "Key Achievements" unless the user's notes explicitly contain such content
8. Output in English Markdown

Notes content:
---
{notes_content}
---

Generate this week's work summary:"""


class BilingualSummarizer:

    def __init__(self, api_key: str = None, model: str = None,
                 max_tokens: int = None, temperature: float = None):
        from config_manager import ConfigManager
        cfg = ConfigManager()
        config = cfg.load()

        self.api_key = api_key or cfg.get_api_key()
        self.base_url = "https://api.deepseek.com"
        self.model = model or config.get("model", "deepseek-v4-pro")
        self.max_tokens = max_tokens or config.get("max_tokens", 4000)
        self.temperature = temperature if temperature is not None else config.get("temperature", 1.0)

    def _call_deepseek(self, prompt: str) -> str:
        try:
            from openai import OpenAI
        except ImportError:
            raise ImportError("请先安装openai库：pip install openai")

        if not self.api_key:
            raise ValueError("请在设置中配置 API Key")

        client = OpenAI(
            api_key=self.api_key,
            base_url=self.base_url
        )

        response = client.chat.completions.create(
            model=self.model,
            messages=[
                {"role": "user", "content": prompt}
            ],
            max_tokens=self.max_tokens,
            temperature=self.temperature
        )

        return response.choices[0].message.content

    def summarize_chinese(self, notes_content: str) -> str:
        prompt = SUMMARY_PROMPT_CN.format(notes_content=notes_content)
        return self._call_deepseek(prompt)

    def summarize_english(self, notes_content: str) -> str:
        prompt = SUMMARY_PROMPT_EN.format(notes_content=notes_content)
        return self._call_deepseek(prompt)

    def summarize_bilingual(self, notes_content: str) -> tuple:
        print("    正在生成中文总结...")
        chinese = self.summarize_chinese(notes_content)
        print("    ✓ 中文总结完成")

        print("    正在生成英文总结...")
        english = self.summarize_english(notes_content)
        print("    ✓ 英文总结完成")

        return chinese, english

    @staticmethod
    def test_connection(api_key: str, model: str = "deepseek-v4-pro") -> tuple:
        """测试 API 连接，返回 (success: bool, message: str)"""
        try:
            from openai import OpenAI
        except ImportError:
            return False, "缺少 openai 库"

        try:
            client = OpenAI(api_key=api_key, base_url="https://api.deepseek.com")
            response = client.chat.completions.create(
                model=model,
                messages=[{"role": "user", "content": "Hi"}],
                max_tokens=5
            )
            return True, "连接成功"
        except Exception as e:
            return False, str(e)
