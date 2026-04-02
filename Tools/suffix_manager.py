import json
import os
import re
import random
import logging

logger = logging.getLogger(__name__)

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
                    content = f.read().strip()
                if not content:
                    raise ValueError("配置文件为空")
                loaded = json.loads(content)
                if not isinstance(loaded, dict):
                    raise ValueError("配置文件格式错误")
                user_suffixes = loaded.get("user_suffixes", {})
                if not isinstance(user_suffixes, dict):
                    user_suffixes = {}
                self.config = {
                    "global_suffix": loaded.get("global_suffix", ""),
                    "user_suffixes": user_suffixes
                }
            except Exception as e:
                logger.error(f"SuffixManager: 加载配置文件失败: {e}")
                self.config = {
                    "global_suffix": "",
                    "user_suffixes": {}
                }
                self.save_config()
        else:
            self.save_config()

    def save_config(self):
        try:
            with open(self.config_file, "w", encoding="utf-8") as f:
                json.dump(self.config, f, indent=4, ensure_ascii=False)
        except Exception as e:
            logger.error(f"SuffixManager: 保存配置文件失败: {e}")

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
        user_id = str(user_id)
        if user_id in self.config["user_suffixes"]:
            return self.config["user_suffixes"][user_id]
        return self.config["global_suffix"]

    def process_text(self, text, user_id):
        suffix = self.get_suffix(user_id)
        if not suffix:
            return text

        if text.endswith(suffix) or text.endswith(suffix + "。") or text.endswith(suffix + "！"):
            return text


        
        processed_text = ""
        i = 0
        n = len(text)
        punctuation = "，。；？！,.;?!"
        
        
        while i < n:
            char = text[i]
            if char in punctuation:
                
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
