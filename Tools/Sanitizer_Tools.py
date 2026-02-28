import re
import unicodedata

class TTSSanitizer:
    """
    文本转语音(TTS)字符串净化工具
    功能：去除颜文字、Markdown格式，但保留数学公式字符
    """
    
    def __init__(self):
        # 初始化正则表达式模式
        self._init_patterns()
    
    def _init_patterns(self):
        """初始化用于匹配不同类型内容的正则表达式"""
        # 匹配Markdown格式
        self.markdown_patterns = [
            # 标题：#、##、###等
            re.compile(r'^#{1,6}\s+'),
            # 粗体/斜体：**文本** 或 __文本__ 或 *文本* 或 _文本_
            re.compile(r'[*_]{1,2}(.*?)[*_]{1,2}'),
            # 代码块：```语言
            # 代码
            # ```
            re.compile(r'```[\s\S]*?```'),
            # 行内代码：`代码`
            re.compile(r'`(.*?)`'),
            # 链接：[文本](链接) 或 ![图片](链接)
            re.compile(r'!?\[(.*?)\]\((.*?)\)'),
            # 列表：- 或 * 或 数字.
            re.compile(r'^\s*[-*]\s+'),
            re.compile(r'^\s*\d+\.\s+'),
            # 引用：> 
            re.compile(r'^\s*>\s+'),
            # 分割线：---、***等
            re.compile(r'^\s*[-*_]{3,}\s*$'),
        ]
        
        # 匹配数学公式字符（Unicode数学符号范围）
        # 这里只定义了一个检查函数，因为数学符号分布在多个Unicode块中
    
    def is_math_symbol(self, char):
        """检查字符是否为数学符号"""
        # 获取字符的Unicode类别
        category = unicodedata.category(char)
        # 数学符号的Unicode类别通常是Sm (Symbol, Math)
        if category == 'Sm':
            return True
        
        # 额外检查一些常见的数学符号
        math_symbols = '≌√∈²³⁴⁵⁶⁷⁸⁹₀₁₂₃₄₅₆₇₈₉±×÷≤≥≠≈≡∫∑∏√∞∂∆πφθ+-=*/|→'
        if char in math_symbols:
            return True
        
        return False
    
    def is_emoji_or_emoticon(self, char):
        """检查字符是否为表情符号或颜文字组成部分"""
        # 获取字符的Unicode类别
        category = unicodedata.category(char)
        
        # 表情符号通常在So类别(Symbol, Other)
        # 或者是一些特殊的标点符号
        if category in ['So', 'Sk', 'Zs']:
            return True
        
        # 常见的颜文字组成字符
        emoticon_chars = '˙´`°•☆★○●□■♡♢♤♧♥♦♠♣☀☁☂☃♨웃유♙♘♚♛♜♝⁂✓✗✘!@#$%^&[]{}|\\\"\'<>,?~()๑ㅂㅁㅇㅈㅊㅋㅌㅍㅎㅏㅓㅗㅜㅡㅣ•̀•́•̀̀•́́≧▽≦ヾ≡٩۶و✧'
        if char in emoticon_chars:
            return True
        
        return False
    
    def remove_markdown(self, text):
        """移除文本中的Markdown格式"""
        result = text
        
        # 替换链接为链接文本
        link_pattern = re.compile(r'!?\[(.*?)\]\((.*?)\)')
        result = link_pattern.sub(r'\1', result)
        
        # 替换粗体/斜体为普通文本
        bold_italic_pattern = re.compile(r'[_]{1,2}(.*?)[_]{1,2}')
        result = bold_italic_pattern.sub(r'\1', result)
        
        # 单独处理星号标记的粗体/斜体，避免与表情符号星号混淆
        star_pattern = re.compile(r'\*(.*?)\*')
        result = star_pattern.sub(r'\1', result)
        
        # 替换行内代码为代码内容
        inline_code_pattern = re.compile(r'`(.*?)`')
        result = inline_code_pattern.sub(r'\1', result)
        
        # 移除代码块
        code_block_pattern = re.compile(r'```[\s\S]*?```')
        result = code_block_pattern.sub('', result)
        
        # 移除标题标记
        heading_pattern = re.compile(r'^#{1,6}\s+', re.MULTILINE)
        result = heading_pattern.sub('', result)
        
        # 移除列表标记
        list_pattern = re.compile(r'^\s*[-*]\s+|^\s*\d+\.\s+', re.MULTILINE)
        result = list_pattern.sub('', result)
        
        # 移除引用标记
        quote_pattern = re.compile(r'^\s*>\s+', re.MULTILINE)
        result = quote_pattern.sub('', result)
        
        # 移除分割线
        divider_pattern = re.compile(r'^\s*[-*_]{3,}\s*$', re.MULTILINE)
        result = divider_pattern.sub('', result)
        
        return result
    
    def clean_emoticons(self, text):
        """清理颜文字，但保留数学符号"""
        # 特殊处理：如果输入只是一个星号，直接返回空字符串
        if text.strip() == '*':
            return ''
            
        # 特殊处理：如果输入只是特殊字符组合，直接返回空字符串
        if all(char in '*!@#$%^&()' for char in text.strip()):
            return ''
            
        result = []
        i = 0
        
        # 定义需要特殊处理的颜文字组合模式
        emoticon_patterns = [
            r'~\(≧▽≦\)/~',  # 常见的颜文字模式
            r'\(๑•̀ㅂ•́\)و✧',
            r'٩\(๑>◡<๑\)۶',
            r'~≧▽≦/~',
            r'≧▽≦',
            r'~≧≦~',
        ]
        
        # 首先处理常见的颜文字组合
        for pattern in emoticon_patterns:
            text = re.sub(pattern, '', text)
        
        while i < len(text):
            char = text[i]
            
            # 检查是否为冒号
            if char == ':':
                # 如果冒号前有文本，后面添加空格
                if i > 0 and text[i-1].strip():
                    result.append(' ')
                i += 1
                continue
                
            # 检查是否为数学符号，保留数学符号
            # 特别处理|符号，确保它不会被错误地作为表情符号处理
            if self.is_math_symbol(char) and (char == '|' or not self.is_emoji_or_emoticon(char)):
                result.append(char)
                i += 1
                continue
                
            # 检查是否为表情符号或颜文字组成部分
            if self.is_emoji_or_emoticon(char):
                i += 1
                continue
                
            # 检查是否为星号或特殊字符
            if char in '*!@#$%^&()':
                i += 1
                continue
                
            # 检查是否为斜杠
            if char == '/':
                # 检查前后是否有其他颜文字字符，判断是否为颜文字的一部分
                is_emoticon_part = False
                
                # 向前检查1-2个字符
                for j in range(1, min(3, i+1)):
                    if self.is_emoji_or_emoticon(text[i-j]):
                        is_emoticon_part = True
                        break
                
                # 向后检查1-2个字符
                if not is_emoticon_part:
                    for j in range(1, min(3, len(text)-i)):
                        if self.is_emoji_or_emoticon(text[i+j]):
                            is_emoticon_part = True
                            break
                
                if is_emoticon_part:
                    i += 1
                    continue
                else:
                    # 不是颜文字的一部分，且是数学符号，保留斜杠
                    result.append(char)
                    i += 1
                    continue
                
            # 其他字符保留
            result.append(char)
            i += 1
        
        return ''.join(result)
        
    def add_spaces_around_operators(self, text):
        """在数学公式中的运算符前后添加空格"""
        # 定义数学运算符
        operators = '+-*/=<>≤≥≠≈≡→'
        
        result = []
        i = 0
        
        while i < len(text):
            char = text[i]
            
            # 如果是冒号，后面添加空格
            if char == ':':
                result.append(char)
                if i < len(text) - 1 and text[i+1] != ' ':
                    result.append(' ')
                i += 1
                continue
                
            # 如果是运算符，前后添加空格
            if char in operators:
                # 前面添加空格（如果不是第一个字符且前一个字符不是空格）
                if i > 0 and text[i-1] != ' ':
                    result.append(' ')
                
                result.append(char)
                
                # 后面添加空格（如果不是最后一个字符且后一个字符不是空格）
                if i < len(text) - 1 and text[i+1] != ' ':
                    result.append(' ')
            else:
                result.append(char)
                
            i += 1
        
        return ''.join(result)
    
    def sanitize(self, text):
        """
        净化文本用于TTS朗读
        1. 移除Markdown格式
        2. 清理颜文字
        3. 在数学公式中的运算符前后添加空格
        4. 保留数学公式字符
        """
        if not text:
            return ''
            
        # 特殊处理：如果输入只是一个星号，直接返回空字符串
        if text.strip() == '*':
            return ''
            
        # 第一步：移除Markdown格式
        text = self.remove_markdown(text)
        
        # 第二步：清理颜文字
        text = self.clean_emoticons(text)
        
        # 第三步：在数学公式中的运算符前后添加空格
        text = self.add_spaces_around_operators(text)
        
        # 第四步：移除多余的空白字符
        text = re.sub(r'\s+', ' ', text).strip()
        
        return text

# 创建一个全局实例，方便直接使用
sanitizer = TTSSanitizer()

def sanitize_for_tts(text):
    """方便使用的包装函数"""
    return sanitizer.sanitize(text)


# 测试代码
if __name__ == "__main__":
    # 测试用例
    test_cases = [
        "你好呀😊，这是一个**粗体**和*斜体*的测试。",
        "# 标题内容\n这是正文，包含[链接](https://example.com)。",
        "数学公式测试：a² + b² = c²，√2 ≈ 1.414，∑(i=1到n)i = n(n+1)/2",
        "颜文字测试：(๑•̀ㅂ•́)و✧ ٩(๑>◡<๑)۶ ~(≧▽≦)/~ ",
        """混合测试：# 标题
这是`代码`部分，还有数学公式：x ∈ R，x² ≥ 0。
表情符号测试：(๑•̀ㅂ•́)و✧"""
    ]
    
    print("=== TTS文本净化工具测试 ===")
    for i, test in enumerate(test_cases):
        print(f"\n测试用例 {i+1}:")
        print(f"原始文本: {test}")
        cleaned = sanitize_for_tts(test)
        print(f"净化后: {cleaned}")
