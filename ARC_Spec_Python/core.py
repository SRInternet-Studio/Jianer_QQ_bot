"""核心模块 - 兼容性保持

为了保持向后兼容性，这个文件保留了原有的功能。
建议使用新的 arcspec_ai.cli 模块。
"""

from arcspec_ai.cli import main

if __name__ == "__main__":
    main()