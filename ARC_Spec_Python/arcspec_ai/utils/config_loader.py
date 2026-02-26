"""配置加载器模块

负责加载和显示AI配置文件。
"""

import os
import json
import logging
from typing import Dict, Any, List

logger = logging.getLogger(__name__)


def load_ai_configs(configs_dir: str = "configs") -> Dict[str, Dict[str, Any]]:
    """加载所有AI配置文件
    
    Args:
        configs_dir: 配置文件目录
        
    Returns:
        配置文件字典，键为文件名（不含扩展名），值为配置内容
    """
    configs = {}
    
    if not os.path.exists(configs_dir):
        logger.error(f"配置目录 {configs_dir} 不存在")
        return configs
    
    logger.info(f"开始加载配置文件，目录: {configs_dir}")
    
    for filename in os.listdir(configs_dir):
        if filename.endswith('.ai.json'):
            filepath = os.path.join(configs_dir, filename)
            try:
                with open(filepath, 'r', encoding='utf-8') as f:
                    config = json.load(f)
                    config_name = filename.replace('.ai.json', '')
                    configs[config_name] = config
                    logger.info(f"已加载配置: {config_name}")
            except Exception as e:
                logger.error(f"加载配置文件 {filename} 失败: {e}")
    
    logger.info(f"配置文件加载完成，共加载 {len(configs)} 个配置")
    return configs


def display_config_list(configs: Dict[str, Dict[str, Any]]) -> List[str]:
    """显示配置文件列表
    
    Args:
        configs: 配置字典
        
    Returns:
        配置名称列表
    """
    if not configs:
        logger.warning("没有找到任何配置文件")
        print("没有找到任何配置文件")
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