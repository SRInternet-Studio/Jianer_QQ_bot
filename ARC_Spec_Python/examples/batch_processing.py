#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
批处理脚本集成示例

这个示例展示了如何将ARC_Spec_Python集成到批处理脚本中，
实现大规模文本处理、数据分析和自动化任务。

特性:
- 批量文件处理
- 多线程并发处理
- 进度跟踪
- 结果统计
- 错误处理和重试
- 输出格式化

依赖:
    pip install tqdm concurrent.futures

运行方式:
    python batch_processing.py --input-dir ./data --output-dir ./results
    python batch_processing.py --input-file data.txt --config mycustom
"""

import sys
import argparse
import json
import csv
from pathlib import Path
from typing import Dict, Any, List, Optional, Tuple
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, asdict
from datetime import datetime
import time
import logging

# 第三方库
try:
    from tqdm import tqdm
except ImportError:
    print("请安装tqdm: pip install tqdm")
    sys.exit(1)

# 添加ARC_Spec_Python到Python路径
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

# 导入ARC_Spec_Python模块
try:
    from arcspec_ai.configurator import load_ai_configs, load_parsers
except ImportError as e:
    print(f"导入ARC_Spec_Python失败: {e}")
    sys.exit(1)

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


@dataclass
class ProcessingTask:
    """处理任务数据类"""
    id: str
    input_text: str
    config_name: str
    source_file: Optional[str] = None
    metadata: Optional[Dict[str, Any]] = None


@dataclass
class ProcessingResult:
    """处理结果数据类"""
    task_id: str
    success: bool
    input_text: str
    output_text: Optional[str] = None
    config_used: Optional[str] = None
    processing_time: Optional[float] = None
    error_message: Optional[str] = None
    timestamp: Optional[str] = None
    metadata: Optional[Dict[str, Any]] = None


class BatchProcessor:
    """批处理器类"""
    
    def __init__(self, project_root: str = None, max_workers: int = 4):
        if project_root is None:
            project_root = Path(__file__).parent.parent
        else:
            project_root = Path(project_root)
            
        self.project_root = project_root
        self.config_dir = project_root / 'configs'
        self.parsers_dir = project_root / 'arcspec_ai' / 'parsers'
        self.max_workers = max_workers
        
        self.configs = {}
        self.parser_registry = None
        self.parsers_cache = {}
        self.initialized = False
        
        # 统计信息
        self.stats = {
            'total_tasks': 0,
            'successful_tasks': 0,
            'failed_tasks': 0,
            'total_processing_time': 0.0,
            'start_time': None,
            'end_time': None
        }
    
    def initialize(self) -> bool:
        """
        初始化批处理器
        
        Returns:
            bool: 是否初始化成功
        """
        try:
            logger.info("初始化批处理器...")
            
            # 加载配置
            self.configs = load_ai_configs(str(self.config_dir))
            if not self.configs:
                raise Exception("未找到任何配置文件")
            
            # 加载解析器
            self.parser_registry = load_parsers(str(self.parsers_dir))
            if not self.parser_registry:
                raise Exception("未找到任何解析器")
            
            # 预加载解析器
            self._preload_parsers()
            
            self.initialized = True
            logger.info(f"批处理器初始化成功，加载了 {len(self.configs)} 个配置")
            
            return True
            
        except Exception as e:
            logger.error(f"初始化失败: {e}")
            return False
    
    def _preload_parsers(self):
        """预加载所有解析器"""
        for config_name, config in self.configs.items():
            try:
                parser = self.parser_registry.create_parser(
                    config['ResponseType'], config
                )
                if parser:
                    self.parsers_cache[config_name] = parser
                    logger.debug(f"预加载解析器: {config_name}")
            except Exception as e:
                logger.error(f"预加载解析器 {config_name} 失败: {e}")
    
    def process_single_task(self, task: ProcessingTask, 
                          retry_count: int = 3) -> ProcessingResult:
        """
        处理单个任务
        
        Args:
            task: 处理任务
            retry_count: 重试次数
            
        Returns:
            ProcessingResult: 处理结果
        """
        start_time = time.time()
        
        for attempt in range(retry_count + 1):
            try:
                if task.config_name not in self.parsers_cache:
                    raise Exception(f"配置 {task.config_name} 不可用")
                
                parser = self.parsers_cache[task.config_name]
                response = parser.parse(task.input_text)
                processing_time = time.time() - start_time
                
                return ProcessingResult(
                    task_id=task.id,
                    success=True,
                    input_text=task.input_text,
                    output_text=response,
                    config_used=task.config_name,
                    processing_time=processing_time,
                    timestamp=datetime.now().isoformat(),
                    metadata=task.metadata
                )
                
            except Exception as e:
                if attempt < retry_count:
                    logger.warning(f"任务 {task.id} 第 {attempt + 1} 次尝试失败，重试中: {e}")
                    time.sleep(1)  # 等待1秒后重试
                    continue
                else:
                    processing_time = time.time() - start_time
                    logger.error(f"任务 {task.id} 处理失败: {e}")
                    
                    return ProcessingResult(
                        task_id=task.id,
                        success=False,
                        input_text=task.input_text,
                        config_used=task.config_name,
                        processing_time=processing_time,
                        error_message=str(e),
                        timestamp=datetime.now().isoformat(),
                        metadata=task.metadata
                    )
    
    def process_batch(self, tasks: List[ProcessingTask], 
                     show_progress: bool = True) -> List[ProcessingResult]:
        """
        批量处理任务
        
        Args:
            tasks: 任务列表
            show_progress: 是否显示进度条
            
        Returns:
            List[ProcessingResult]: 处理结果列表
        """
        if not self.initialized:
            raise Exception("批处理器未初始化")
        
        self.stats['total_tasks'] = len(tasks)
        self.stats['start_time'] = datetime.now()
        
        results = []
        
        logger.info(f"开始批量处理 {len(tasks)} 个任务，使用 {self.max_workers} 个线程")
        
        # 使用线程池并发处理
        with ThreadPoolExecutor(max_workers=self.max_workers) as executor:
            # 提交所有任务
            future_to_task = {
                executor.submit(self.process_single_task, task): task 
                for task in tasks
            }
            
            # 处理完成的任务
            if show_progress:
                progress_bar = tqdm(total=len(tasks), desc="处理进度")
            
            for future in as_completed(future_to_task):
                try:
                    result = future.result()
                    results.append(result)
                    
                    # 更新统计信息
                    if result.success:
                        self.stats['successful_tasks'] += 1
                    else:
                        self.stats['failed_tasks'] += 1
                    
                    if result.processing_time:
                        self.stats['total_processing_time'] += result.processing_time
                    
                    if show_progress:
                        progress_bar.update(1)
                        progress_bar.set_postfix({
                            '成功': self.stats['successful_tasks'],
                            '失败': self.stats['failed_tasks']
                        })
                        
                except Exception as e:
                    logger.error(f"处理任务时发生异常: {e}")
                    self.stats['failed_tasks'] += 1
                    
                    if show_progress:
                        progress_bar.update(1)
            
            if show_progress:
                progress_bar.close()
        
        self.stats['end_time'] = datetime.now()
        
        logger.info(f"批量处理完成，成功: {self.stats['successful_tasks']}, "
                   f"失败: {self.stats['failed_tasks']}")
        
        return results
    
    def load_tasks_from_file(self, file_path: str, config_name: str,
                           text_column: str = 'text') -> List[ProcessingTask]:
        """
        从文件加载任务
        
        Args:
            file_path: 文件路径
            config_name: 配置名称
            text_column: 文本列名（对于CSV文件）
            
        Returns:
            List[ProcessingTask]: 任务列表
        """
        file_path = Path(file_path)
        tasks = []
        
        try:
            if file_path.suffix.lower() == '.json':
                # JSON文件
                with open(file_path, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                
                if isinstance(data, list):
                    for i, item in enumerate(data):
                        if isinstance(item, dict):
                            text = item.get(text_column, str(item))
                            metadata = {k: v for k, v in item.items() if k != text_column}
                        else:
                            text = str(item)
                            metadata = {}
                        
                        tasks.append(ProcessingTask(
                            id=f"{file_path.stem}_{i}",
                            input_text=text,
                            config_name=config_name,
                            source_file=str(file_path),
                            metadata=metadata
                        ))
                else:
                    tasks.append(ProcessingTask(
                        id=f"{file_path.stem}_0",
                        input_text=str(data),
                        config_name=config_name,
                        source_file=str(file_path)
                    ))
            
            elif file_path.suffix.lower() == '.csv':
                # CSV文件
                with open(file_path, 'r', encoding='utf-8') as f:
                    reader = csv.DictReader(f)
                    for i, row in enumerate(reader):
                        text = row.get(text_column, '')
                        if not text:
                            continue
                        
                        metadata = {k: v for k, v in row.items() if k != text_column}
                        
                        tasks.append(ProcessingTask(
                            id=f"{file_path.stem}_{i}",
                            input_text=text,
                            config_name=config_name,
                            source_file=str(file_path),
                            metadata=metadata
                        ))
            
            else:
                # 文本文件
                with open(file_path, 'r', encoding='utf-8') as f:
                    lines = f.readlines()
                
                for i, line in enumerate(lines):
                    line = line.strip()
                    if line:
                        tasks.append(ProcessingTask(
                            id=f"{file_path.stem}_{i}",
                            input_text=line,
                            config_name=config_name,
                            source_file=str(file_path)
                        ))
            
            logger.info(f"从文件 {file_path} 加载了 {len(tasks)} 个任务")
            return tasks
            
        except Exception as e:
            logger.error(f"加载任务文件失败: {e}")
            return []
    
    def load_tasks_from_directory(self, dir_path: str, config_name: str,
                                file_pattern: str = '*.txt') -> List[ProcessingTask]:
        """
        从目录加载任务
        
        Args:
            dir_path: 目录路径
            config_name: 配置名称
            file_pattern: 文件模式
            
        Returns:
            List[ProcessingTask]: 任务列表
        """
        dir_path = Path(dir_path)
        tasks = []
        
        try:
            files = list(dir_path.glob(file_pattern))
            logger.info(f"在目录 {dir_path} 中找到 {len(files)} 个文件")
            
            for file_path in files:
                file_tasks = self.load_tasks_from_file(str(file_path), config_name)
                tasks.extend(file_tasks)
            
            logger.info(f"从目录 {dir_path} 总共加载了 {len(tasks)} 个任务")
            return tasks
            
        except Exception as e:
            logger.error(f"加载任务目录失败: {e}")
            return []
    
    def save_results(self, results: List[ProcessingResult], 
                    output_path: str, format: str = 'json'):
        """
        保存处理结果
        
        Args:
            results: 处理结果列表
            output_path: 输出路径
            format: 输出格式 ('json', 'csv', 'txt')
        """
        output_path = Path(output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        
        try:
            if format.lower() == 'json':
                # JSON格式
                data = [asdict(result) for result in results]
                with open(output_path, 'w', encoding='utf-8') as f:
                    json.dump(data, f, ensure_ascii=False, indent=2)
            
            elif format.lower() == 'csv':
                # CSV格式
                with open(output_path, 'w', newline='', encoding='utf-8') as f:
                    if results:
                        fieldnames = list(asdict(results[0]).keys())
                        writer = csv.DictWriter(f, fieldnames=fieldnames)
                        writer.writeheader()
                        
                        for result in results:
                            row = asdict(result)
                            # 处理复杂字段
                            if row['metadata']:
                                row['metadata'] = json.dumps(row['metadata'], ensure_ascii=False)
                            writer.writerow(row)
            
            else:
                # 文本格式
                with open(output_path, 'w', encoding='utf-8') as f:
                    f.write(f"批处理结果报告\n")
                    f.write(f"生成时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
                    f.write("=" * 50 + "\n\n")
                    
                    for result in results:
                        f.write(f"任务ID: {result.task_id}\n")
                        f.write(f"状态: {'成功' if result.success else '失败'}\n")
                        f.write(f"输入: {result.input_text[:100]}...\n")
                        
                        if result.success:
                            f.write(f"输出: {result.output_text[:200]}...\n")
                            f.write(f"配置: {result.config_used}\n")
                            f.write(f"处理时间: {result.processing_time:.2f}s\n")
                        else:
                            f.write(f"错误: {result.error_message}\n")
                        
                        f.write("-" * 30 + "\n\n")
            
            logger.info(f"结果已保存到: {output_path}")
            
        except Exception as e:
            logger.error(f"保存结果失败: {e}")
    
    def print_statistics(self):
        """打印统计信息"""
        if self.stats['start_time'] and self.stats['end_time']:
            duration = (self.stats['end_time'] - self.stats['start_time']).total_seconds()
        else:
            duration = 0
        
        print("\n" + "=" * 50)
        print("批处理统计信息")
        print("=" * 50)
        print(f"总任务数: {self.stats['total_tasks']}")
        print(f"成功任务: {self.stats['successful_tasks']}")
        print(f"失败任务: {self.stats['failed_tasks']}")
        print(f"成功率: {self.stats['successful_tasks'] / max(self.stats['total_tasks'], 1) * 100:.1f}%")
        print(f"总处理时间: {self.stats['total_processing_time']:.2f}s")
        print(f"总耗时: {duration:.2f}s")
        
        if self.stats['successful_tasks'] > 0:
            avg_time = self.stats['total_processing_time'] / self.stats['successful_tasks']
            print(f"平均处理时间: {avg_time:.2f}s")
        
        if duration > 0:
            throughput = self.stats['total_tasks'] / duration
            print(f"处理速度: {throughput:.2f} 任务/秒")
        
        print("=" * 50)


def create_sample_data(output_dir: str):
    """创建示例数据文件"""
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    
    # 创建文本文件
    with open(output_dir / 'sample.txt', 'w', encoding='utf-8') as f:
        f.write("这是第一个测试文本\n")
        f.write("这是第二个测试文本\n")
        f.write("这是第三个测试文本\n")
    
    # 创建JSON文件
    sample_data = [
        {"text": "分析这段文本的情感", "category": "sentiment"},
        {"text": "总结这篇文章的要点", "category": "summary"},
        {"text": "翻译这句话到英文", "category": "translation"}
    ]
    
    with open(output_dir / 'sample.json', 'w', encoding='utf-8') as f:
        json.dump(sample_data, f, ensure_ascii=False, indent=2)
    
    # 创建CSV文件
    with open(output_dir / 'sample.csv', 'w', newline='', encoding='utf-8') as f:
        writer = csv.writer(f)
        writer.writerow(['text', 'priority', 'source'])
        writer.writerow(['处理这个重要文档', 'high', 'document1'])
        writer.writerow(['分析用户反馈', 'medium', 'feedback'])
        writer.writerow(['生成报告摘要', 'low', 'report'])
    
    print(f"示例数据已创建在: {output_dir}")


def main():
    """主函数"""
    parser = argparse.ArgumentParser(description='ARC_Spec_Python 批处理脚本')
    
    # 输入选项
    input_group = parser.add_mutually_exclusive_group(required=True)
    input_group.add_argument('--input-file', help='输入文件路径')
    input_group.add_argument('--input-dir', help='输入目录路径')
    input_group.add_argument('--create-sample', help='创建示例数据到指定目录')
    
    # 其他选项
    parser.add_argument('--config', default='mycustom', help='配置名称 (默认: mycustom)')
    parser.add_argument('--output-dir', default='./batch_results', help='输出目录 (默认: ./batch_results)')
    parser.add_argument('--output-format', choices=['json', 'csv', 'txt'], default='json', help='输出格式 (默认: json)')
    parser.add_argument('--text-column', default='text', help='文本列名 (对于CSV文件, 默认: text)')
    parser.add_argument('--file-pattern', default='*.txt', help='文件模式 (对于目录输入, 默认: *.txt)')
    parser.add_argument('--max-workers', type=int, default=4, help='最大工作线程数 (默认: 4)')
    parser.add_argument('--no-progress', action='store_true', help='不显示进度条')
    parser.add_argument('--verbose', '-v', action='store_true', help='详细输出')
    
    args = parser.parse_args()
    
    # 设置日志级别
    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)
    
    # 创建示例数据
    if args.create_sample:
        create_sample_data(args.create_sample)
        return
    
    # 创建批处理器
    processor = BatchProcessor(max_workers=args.max_workers)
    
    # 初始化
    if not processor.initialize():
        print("初始化失败")
        sys.exit(1)
    
    # 检查配置是否存在
    if args.config not in processor.configs:
        print(f"配置 '{args.config}' 不存在")
        print(f"可用配置: {list(processor.configs.keys())}")
        sys.exit(1)
    
    # 加载任务
    tasks = []
    
    if args.input_file:
        tasks = processor.load_tasks_from_file(
            args.input_file, 
            args.config, 
            args.text_column
        )
    elif args.input_dir:
        tasks = processor.load_tasks_from_directory(
            args.input_dir, 
            args.config, 
            args.file_pattern
        )
    
    if not tasks:
        print("没有找到任务")
        sys.exit(1)
    
    print(f"加载了 {len(tasks)} 个任务")
    
    # 处理任务
    results = processor.process_batch(
        tasks, 
        show_progress=not args.no_progress
    )
    
    # 保存结果
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    
    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    output_file = output_dir / f"batch_results_{timestamp}.{args.output_format}"
    
    processor.save_results(results, str(output_file), args.output_format)
    
    # 打印统计信息
    processor.print_statistics()
    
    print(f"\n处理完成！结果已保存到: {output_file}")


if __name__ == '__main__':
    main()