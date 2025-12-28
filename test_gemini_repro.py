import asyncio
import logging
import sys
import os

# 设置日志
logging.basicConfig(level=logging.DEBUG)

# 添加项目路径
sys.path.append(os.getcwd())
sys.path.append(os.path.join(os.getcwd(), "ARC_Spec_Python")) # 恢复 ARC_Spec_Python 路径

from parser.gemini import GeminiParser

async def test():
    config = {
        "FriendlyName": "Gemini",
        "Model": "gemini-2.5-flash", 
        "ResponseType": "gemini",
        "APIKey": "AIzaSyCAn2llRSayZSKddUKpzw67B-DyzI6lyNs",
        "BaseUrl": "https://gemini2.moonpeach.top/v1beta",
        "Temperature": 1,
        "MaxTokens": 8192
    }
    
    print(f"Testing with config: {config}")
    
    parser = GeminiParser(config)
    
    try:
        print("Sending request: '肾助拳义和团' (Triggering safety filter likely)")
        response = await parser._chat_async("肾助拳义和团")
        print(f"Final Response: {response}")
    except Exception as e:
        print(f"Error: {e}")

if __name__ == "__main__":
    asyncio.run(test())
