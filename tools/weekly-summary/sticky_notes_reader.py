# -*- coding: utf-8 -*-
"""
Windows便笺读取器
读取Windows Sticky Notes的SQLite数据库，提取指定便笺内容
支持自动识别活跃项目便笺
"""

import sqlite3
import os
import re
from datetime import datetime, timedelta
from pathlib import Path


# 配置常量
CONFIG = {
    "active_days_threshold": 14,  # 活跃天数阈值（两周）
    "always_include": ["本周计划"],  # 始终包含的便笺
    "always_exclude": [],  # 始终排除的便笺
}

# 分类模式
CATEGORY_PATTERNS = {
    "项目": [
        r"^P\d+",            # P0114-xxx 格式（新命名约定）
        r"^PC\d+",           # PC047 格式（旧项目兼容）
    ],
    "技能训练": [
        r"^R\s+\d{2}-\d{2}", # R 01-12 格式
    ],
    "本周计划": [
        r"本周计划",
    ],
}


def extract_date_ranges(text: str) -> list:
    """从内容中提取 MM-DD ~ MM-DD 格式的日期范围

    返回所有找到的结束日期列表（datetime对象）
    """
    if not text:
        return []

    # 匹配 MM-DD ~ MM-DD 或 MM-DD~MM-DD 格式
    pattern = r'(\d{2})-(\d{2})\s*[~～]\s*(\d{2})-(\d{2})'
    matches = re.findall(pattern, text)

    dates = []
    current_year = datetime.now().year

    for match in matches:
        end_month, end_day = int(match[2]), int(match[3])
        try:
            # 使用结束日期作为判断依据
            end_date = datetime(current_year, end_month, end_day)
            # 如果日期在未来超过6个月，可能是去年的记录
            if end_date > datetime.now() + timedelta(days=180):
                end_date = datetime(current_year - 1, end_month, end_day)
            dates.append(end_date)
        except ValueError:
            continue

    return dates


def is_within_days(date: datetime, threshold: int) -> bool:
    """判断日期是否在阈值天数内"""
    if not date:
        return False
    return (datetime.now() - date).days <= threshold


def is_active_note(note: dict, config: dict = CONFIG) -> bool:
    """综合判断便笺是否活跃

    判断逻辑：
    1. 如果在 always_include 中，返回 True
    2. 如果在 always_exclude 中，返回 False
    3. 如果内容中有近期日期范围，返回 True
    4. 如果 UpdatedAt 在阈值天数内，返回 True
    5. 否则返回 False
    """
    text = note.get('text_clean', '')
    first_line = text.split('\n')[0].strip() if text else ""
    threshold = config.get('active_days_threshold', 14)

    # 检查 always_include
    for keyword in config.get('always_include', []):
        if keyword in first_line:
            return True

    # 检查 always_exclude
    for keyword in config.get('always_exclude', []):
        if keyword in first_line:
            return False

    # 检查内容中的日期范围
    date_ranges = extract_date_ranges(text)
    for date in date_ranges:
        if is_within_days(date, threshold):
            return True

    # 检查更新时间
    updated_at = note.get('updated_at')
    if updated_at and is_within_days(updated_at, threshold):
        return True

    return False


def classify_note(title: str) -> str:
    """根据标题分类便笺类型

    分类逻辑：
    1. 优先匹配"技能训练"和"本周计划"
    2. 如果不匹配，默认归为"项目"（活跃便笺都是有价值的）
    """
    # 优先检查特定分类
    for category in ["技能训练", "本周计划"]:
        if category in CATEGORY_PATTERNS:
            for pattern in CATEGORY_PATTERNS[category]:
                if re.search(pattern, title):
                    return category

    # 默认归为项目（让活跃便笺都能被记录）
    return "项目"


class StickyNotesReader:
    """Windows便笺读取器"""

    def __init__(self):
        """初始化，获取便笺数据库路径"""
        local_app_data = os.environ.get('LOCALAPPDATA', '')
        self.db_path = Path(local_app_data) / \
            "Packages/Microsoft.MicrosoftStickyNotes_8wekyb3d8bbwe/LocalState/plum.sqlite"

        if not self.db_path.exists():
            raise FileNotFoundError(f"找不到便笺数据库: {self.db_path}")

    def _clean_text(self, text: str) -> str:
        """清理RTF格式标记，提取纯文本"""
        if not text:
            return ""

        # 移除 \id=xxx 标记
        text = re.sub(r'\\id=[a-f0-9-]+\s*', '', text)

        # 移除RTF格式标记 \b \b0 \l \l0 等
        text = re.sub(r'\\[a-z]+[0-9]*\s*', '', text)

        # 清理多余空白
        text = re.sub(r'\n\s*\n', '\n', text)
        text = text.strip()

        return text

    def _filetime_to_datetime(self, filetime: int) -> datetime:
        """将Windows FILETIME转换为datetime"""
        # FILETIME是从1601-01-01开始的100纳秒间隔数
        # 转换为Python datetime
        if not filetime:
            return None

        # 1601到1970的秒数差
        EPOCH_DIFF = 11644473600
        timestamp = filetime / 10000000 - EPOCH_DIFF

        try:
            return datetime.fromtimestamp(timestamp)
        except:
            return None

    def get_all_notes(self) -> list:
        """获取所有未删除的便笺"""
        conn = sqlite3.connect(str(self.db_path))
        cursor = conn.cursor()

        cursor.execute("""
            SELECT Id, Text, CreatedAt, UpdatedAt
            FROM Note
            WHERE DeletedAt IS NULL
            ORDER BY UpdatedAt DESC
        """)

        notes = []
        for row in cursor.fetchall():
            note_id, text, created_at, updated_at = row
            notes.append({
                'id': note_id,
                'text': text,
                'text_clean': self._clean_text(text),
                'created_at': self._filetime_to_datetime(created_at),
                'updated_at': self._filetime_to_datetime(updated_at)
            })

        conn.close()
        return notes

    def get_target_notes(self) -> dict:
        """获取目标便笺（工作相关的活跃便笺）

        使用自动识别逻辑：
        1. 过滤活跃便笺（基于日期范围和更新时间）
        2. 自动分类（项目/技能训练/本周计划）
        """
        all_notes = self.get_all_notes()
        target_notes = {}

        for note in all_notes:
            # 检查是否活跃
            if not is_active_note(note):
                continue

            text = note['text_clean']
            first_line = text.split('\n')[0].strip() if text else ""

            # 分类便笺
            category = classify_note(first_line)
            if not category:
                continue

            # 构建便笺数据
            note_data = {
                'title': first_line,
                'content': text,
                'updated_at': note['updated_at'],
                'id': note['id'],
                'category': category
            }

            # 技能训练类只保留一个（最新的）
            if category == "技能训练":
                if "R训练" not in target_notes:
                    target_notes["R训练"] = note_data
            # 本周计划只保留一个
            elif category == "本周计划":
                if "本周计划" not in target_notes:
                    target_notes["本周计划"] = note_data
            # 项目类可以有多个，使用标题作为key
            else:
                target_notes[first_line] = note_data

        return target_notes

    def _datetime_to_filetime(self, dt: datetime) -> int:
        """将datetime转换为Windows FILETIME"""
        EPOCH_DIFF = 11644473600
        timestamp = dt.timestamp() + EPOCH_DIFF
        return int(timestamp * 10000000)

    def append_to_note(self, note_id: str, text: str):
        """在指定便笺末尾追加文本"""
        conn = sqlite3.connect(str(self.db_path))
        cursor = conn.cursor()

        cursor.execute("SELECT Text FROM Note WHERE Id = ?", (note_id,))
        row = cursor.fetchone()
        if not row:
            conn.close()
            raise ValueError(f"便笺 {note_id} 不存在")

        current_text = row[0] or ""
        new_text = current_text.rstrip() + "\n" + text
        now_filetime = self._datetime_to_filetime(datetime.now())

        cursor.execute(
            "UPDATE Note SET Text = ?, UpdatedAt = ? WHERE Id = ?",
            (new_text, now_filetime, note_id)
        )
        conn.commit()
        conn.close()

    def format_notes_for_summary(self) -> str:
        """格式化便笺内容，准备用于AI总结"""
        target_notes = self.get_target_notes()

        output = []
        output.append("=" * 60)
        output.append("本周工作记录汇总")
        output.append(f"生成时间: {datetime.now().strftime('%Y-%m-%d %H:%M')}")
        output.append("=" * 60)
        output.append("")

        # 项目类便笺（动态获取所有活跃项目）
        output.append("【项目进展】")
        output.append("-" * 40)

        project_notes = [
            (key, note) for key, note in target_notes.items()
            if note.get('category') == '项目'
        ]

        if project_notes:
            for key, note in project_notes:
                output.append(f"\n## {note['title']}")
                output.append(f"最后更新: {note['updated_at'].strftime('%Y-%m-%d %H:%M') if note['updated_at'] else '未知'}")
                output.append(note['content'])
                output.append("")
        else:
            output.append("\n（无活跃项目便笺）")
            output.append("")

        # R训练便笺
        output.append("\n【技能训练】")
        output.append("-" * 40)
        if "R训练" in target_notes:
            note = target_notes["R训练"]
            output.append(f"\n## R/编程训练")
            output.append(f"最后更新: {note['updated_at'].strftime('%Y-%m-%d %H:%M') if note['updated_at'] else '未知'}")
            output.append(note['content'])
        else:
            output.append("\n（无活跃技能训练便笺）")

        # 本周计划
        output.append("\n【本周计划】")
        output.append("-" * 40)
        if "本周计划" in target_notes:
            note = target_notes["本周计划"]
            output.append(f"最后更新: {note['updated_at'].strftime('%Y-%m-%d %H:%M') if note['updated_at'] else '未知'}")
            output.append(note['content'])
        else:
            output.append("\n（无本周计划便笺）")

        return "\n".join(output)


def main():
    """主函数：读取并显示便笺内容"""
    import sys
    # 设置stdout编码为utf-8，避免Windows控制台编码问题
    if sys.platform == 'win32':
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')

    try:
        reader = StickyNotesReader()
        content = reader.format_notes_for_summary()
        print(content)

        # 保存到文件
        output_file = Path(__file__).parent / "weekly_notes.txt"
        with open(output_file, 'w', encoding='utf-8') as f:
            f.write(content)
        print(f"\n\n便笺内容已保存至: {output_file}")

    except Exception as e:
        print(f"错误: {e}")


if __name__ == "__main__":
    main()
