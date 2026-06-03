import json
import re
import os

from openai import OpenAI
from config import DEEPSEEK_API_KEY, OPENAI_BASE_URL, QUESTION_BANK_FILE

client = OpenAI(api_key=DEEPSEEK_API_KEY, base_url=OPENAI_BASE_URL)


def extract_json_from_response(result_text: str):
    if "```json" in result_text:
        result_text = result_text.split("```json")[1].split("```")[0]
    elif "```" in result_text:
        result_text = result_text.split("```")[1].split("```")[0]
    return json.loads(result_text.strip())


def generate_questions(resume_text: str) -> list:
    if os.getenv("RESUME_QUESTION_MODE") == "ai":
        prompt = f"""你是一位资深 HR 和技术面试官。请仔细阅读以下候选人的简历内容，生成 7 个有深度的、有针对性的面试问题。问题应：
1. 针对简历中的技术栈、项目经验、工作经历来提问
2. 由浅入深排列（前2个关于项目和经历，后5个关于技术深度）
3. 每个问题不超过 40 个字
4. 使用中文提问

简历内容：
{resume_text[:3000]}

请只输出 JSON 数组格式，不要有其他内容：
["问题1", "问题2", "问题3", "问题4", "问题5", "问题6", "问题7"]"""
        try:
            response = client.chat.completions.create(
                model="deepseek-chat",
                messages=[
                    {"role": "system", "content": "你是一位资深 HR 和技术面试官，只输出 JSON 数组格式。"},
                    {"role": "user", "content": prompt}
                ],
                temperature=0.7,
                timeout=30,
            )
            result_text = response.choices[0].message.content
            return extract_json_from_response(result_text)
        except Exception:
            pass
    return [
        "请介绍一下你在最近一个项目中的技术栈和具体贡献。",
        "你在简历中提到熟悉相关技能，能谈谈你使用过的框架或工具吗？",
        "你解决过最复杂的技术问题是什么？",
        "你如何看待团队协作中的代码审查？",
        "你对未来三年职业发展的规划是什么？",
        "你如何跟踪和学习新技术？请举例说明。",
        "当你遇到一个从未遇到的技术难题时，你的解决步骤是什么？"
    ]


def load_question_bank():
    try:
        with open(QUESTION_BANK_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data
    except Exception as e:
        return {"error": f"题库加载失败：{str(e)}"}


def clean_resume_text(text: str) -> str:
    text = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]', '', text)
    text = re.sub(r'[^\u4e00-\u9fff\u3000-\u303f\uff00-\uffefa-zA-Z0-9\s.,;:!?()（）【】\[\]{}《》<>/\\\-_+=@#$%^&*|~`"\']', '', text)
    text = re.sub(r'\s+', ' ', text).strip()
    return text
