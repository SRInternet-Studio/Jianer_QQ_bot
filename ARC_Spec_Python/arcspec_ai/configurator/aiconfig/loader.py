"""AI配置加载器模块

负责加载、验证和显示AI配置文件。
"""

import os
import json
import logging
from typing import Dict, Any, List

logger = logging.getLogger(__name__)

# 必需字段验证规则
REQUIRED_FIELDS = {
    "FriendlyName": str,
    "Model": str,
    "ResponseType": str,
    "Temperature": (int, float),
    "MaxTokens": int
}

# 可选字段验证规则
OPTIONAL_FIELDS = {
    "Introduction": str,
    "Personality": str,
    "max_history_tokens": int,
    "max_history_messages": int,
    "it_multimodal_model": str
}


def validate_config(config: Dict[str, Any]) -> bool:
    """验证配置文件格式
    
    Args:
        config: 待验证的配置字典
        
    Returns:
        配置是否有效
    """
    try:
        # 检查必需字段
        for field, expected_type in REQUIRED_FIELDS.items():
            if field not in config:
                logger.error(f"配置缺少必需字段: {field}")
                return False
            
            if not isinstance(config[field], expected_type):
                logger.error(f"字段 {field} 类型错误，期望 {expected_type}，实际 {type(config[field])}")
                return False
        
        # 检查可选字段类型
        for field, expected_type in OPTIONAL_FIELDS.items():
            if field in config and not isinstance(config[field], expected_type):
                logger.error(f"可选字段 {field} 类型错误，期望 {expected_type}，实际 {type(config[field])}")
                return False
        
        # 验证数值范围
        if config.get("Temperature") is not None:
            temp = config["Temperature"]
            if not (0.0 <= temp <= 2.0):
                logger.error(f"Temperature 值 {temp} 超出范围 [0.0, 2.0]")
                return False
        
        if config.get("MaxTokens") is not None:
            max_tokens = config["MaxTokens"]
            if max_tokens <= 0:
                logger.error(f"MaxTokens 值 {max_tokens} 必须大于0")
                return False
        
        return True
        
    except Exception as e:
        logger.error(f"配置验证过程中发生错误: {e}")
        return False


def load(config_dir: str) -> Dict[str, Dict[str, Any]]:
    """加载指定目录下的所有AI配置文件
    
    Args:
        config_dir: 配置文件所在目录路径
        
    Returns:
        配置名称到配置内容的映射
    """
    configs = {}
    
    if not os.path.exists(config_dir):
        logger.error(f"配置目录 {config_dir} 不存在")
        return configs
    
    if not os.path.isdir(config_dir):
        logger.error(f"路径 {config_dir} 不是一个目录")
        return configs
    
    logger.info(f"开始加载配置文件，目录: {config_dir}")
    
    for filename in os.listdir(config_dir):
        if filename.endswith('.ai.json'):
            filepath = os.path.join(config_dir, filename)
            try:
                with open(filepath, 'r', encoding='utf-8') as f:
                    config = json.load(f)
                    
                # 验证配置格式
                if not validate_config(config):
                    logger.warning(f"配置文件 {filename} 格式验证失败，跳过加载")
                    continue
                    
                config_name = filename.replace('.ai.json', '')
                configs[config_name] = config
                logger.info(f"已加载配置: {config_name}")
                
            except json.JSONDecodeError as e:
                logger.error(f"配置文件 {filename} JSON格式错误: {e}")
            except Exception as e:
                logger.error(f"加载配置文件 {filename} 失败: {e}")
    
    logger.info(f"配置文件加载完成，共加载 {len(configs)} 个有效配置")
    return configs


def display_config_list(configs: Dict[str, Dict[str, Any]]) -> List[str]:
    """显示配置文件列表
    
    Args:
        configs: 配置字典
        
    Returns:
        配置名称列表
    """
    if not configs:
        logger.warning("没有找到任何有效的配置文件")
        print("没有找到任何有效的配置文件")
        return []
    
    print("\n" + "=" * 80)
    print("可用的AI配置文件:")
    print("=" * 80)
    print(f"{'序号':<4} {'FriendlyName':<20} {'Model':<25} {'Introduction':<30}")
    print("-" * 80)
    
    config_names = list(configs.keys())
    for i, (config_name, config_data) in enumerate(configs.items(), 1):
        friendly_name = config_data.get('FriendlyName', config_name)
        model = config_data.get('Model', 'Unknown')
        introduction = config_data.get('Introduction', 'No description')
        
        # 截断过长的文本
        if len(friendly_name) > 18:
            friendly_name = friendly_name[:15] + "..."
        if len(model) > 23:
            model = model[:20] + "..."
        if len(introduction) > 28:
            introduction = introduction[:25] + "..."
            
        print(f"{i:<4} {friendly_name:<20} {model:<25} {introduction:<30}")
    
    print("=" * 80)
    logger.info(f"显示了 {len(config_names)} 个配置文件")
    return config_names