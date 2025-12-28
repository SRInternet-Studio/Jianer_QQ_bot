import json
import os
import re
import random

class SuffixManager:
    def __init__(self, config_file="suffix_config.json"):
        self.config_file = config_file
        self.config = {
            "global_suffix": "",
            "user_suffixes": {}
        }
        self.load_config()

    def load_config(self):
        if os.path.exists(self.config_file):
            try:
                with open(self.config_file, "r", encoding="utf-8") as f:
                    self.config = json.load(f)
            except Exception as e:
                print(f"SuffixManager: 加载配置文件失败: {e}")
        else:
            self.save_config()

    def save_config(self):
        try:
            with open(self.config_file, "w", encoding="utf-8") as f:
                json.dump(self.config, f, indent=4, ensure_ascii=False)
        except Exception as e:
            print(f"SuffixManager: 保存配置文件失败: {e}")

    def set_global_suffix(self, suffix):
        self.config["global_suffix"] = suffix
        self.save_config()

    def set_user_suffix(self, user_id, suffix):
        self.config["user_suffixes"][str(user_id)] = suffix
        self.save_config()

    def remove_global_suffix(self):
        self.config["global_suffix"] = ""
        self.save_config()

    def remove_user_suffix(self, user_id):
        if str(user_id) in self.config["user_suffixes"]:
            del self.config["user_suffixes"][str(user_id)]
            self.save_config()

    def get_suffix(self, user_id):
        # 优先使用特定后缀，如果没有则使用全局后缀
        user_id = str(user_id)
        if user_id in self.config["user_suffixes"]:
            return self.config["user_suffixes"][user_id]
        return self.config["global_suffix"]

    def process_text(self, text, user_id):
        suffix = self.get_suffix(user_id)
        if not suffix:
            return text

        # 避免重复添加后缀 (简单的检查)
        if text.endswith(suffix) or text.endswith(suffix + "。") or text.endswith(suffix + "！"):
            return text

        # 定义标点符号正则
        # 匹配中文和英文的逗号、句号、分号、问号、感叹号
        # 排除已经是后缀+标点的情况（稍微复杂，这里先做简单替换）
        
        # 策略：在标点符号前插入后缀
        # 使用 lookahead 断言，匹配标点符号
        # pattern = r'(?=[，。；？！,.;?!])'
        # 但是 Python re 模块的 lookahead 不消耗字符，我们直接替换标点符号
        
        # 分割文本，避免在代码块或特殊格式中替换（这里暂不处理复杂markdown，假设主要是纯文本）
        
        # 1. 替换标点符号前的空位？不，直接替换标点符号为 "后缀+标点"
        # 排除掉 "..." 这种省略号，或者连续的标点
        
        processed_text = ""
        i = 0
        n = len(text)
        punctuation = "，。；？！,.;?!"
        
        # 简单算法：遍历字符串
        # 如果遇到标点，且前一个字符不是标点，且前面没有已经是后缀了，则插入
        
        while i < n:
            char = text[i]
            if char in punctuation:
                # 检查是否是连续标点的一部分（例如 "..." 或 "!!")
                # 如果是连续标点，只在第一个标点前加？或者在最后一个标点后加？
                # 用户说“在逗号、句号...前”，通常指句意结束处。
                # 简化处理：只要遇到标点，就在它前面加后缀。
                # 但要防止重复：如果前面已经是后缀了，就不加。
                
                # 检查前缀
                is_suffix_already = False
                if i >= len(suffix):
                    if text[i-len(suffix):i] == suffix:
                        is_suffix_already = True
                
                if not is_suffix_already:
                     processed_text += suffix
                
                processed_text += char
            else:
                processed_text += char
            i += 1
            
        # 处理句尾没有标点的情况
        if n > 0 and text[-1] not in punctuation:
            if not text.endswith(suffix):
                processed_text += suffix
                
        return processed_text

