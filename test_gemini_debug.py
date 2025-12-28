import asyncio
import logging
import sys
import os

# 设置日志
logging.basicConfig(level=logging.DEBUG)

# 添加项目路径
sys.path.append(os.getcwd())
sys.path.append(os.path.join(os.getcwd(), "ARC_Spec_Python")) # 添加 ARC_Spec_Python 路径

from parser.gemini import GeminiParser

async def test():
    config = {
        "FriendlyName": "Gemini",
        "Model": "gemini-2.0-flash-exp", # 尝试使用标准模型名，防止 gemini-2.5-flash 不存在导致问题，或者使用配置文件中的
        "ResponseType": "gemini",
        "APIKey": "AIzaSyCAn2llRSayZSKddUKpzw67B-DyzI6lyNs",
        "BaseUrl": "https://gemini2.moonpeach.top/v1beta",
        "Temperature": 1,
        "MaxTokens": 8192
    }
    
    # 使用配置文件中的模型名
    config["Model"] = "gemini-2.0-flash-exp" 
    
    print(f"Testing with config: {config}")
    
    parser = GeminiParser(config)
    
    try:
        print("Sending request...")
        response = await parser._chat_async("你好")
        print(f"Response: {response}")
    except Exception as e:
        print(f"Error: {e}")

if __name__ == "__main__":
    asyncio.run(test())
