import re
from qfluentwidgets import LineEdit, TextEdit

from PySide6.QtWidgets import QTextBrowser, QApplication
from PySide6.QtCore import Qt
from qfluentwidgets.components.widgets.line_edit import EditLayer, SmoothScrollDelegate
from qfluentwidgets.components.widgets.menu import TextEditMenu
from qfluentwidgets.common.style_sheet import FluentStyleSheet
from qfluentwidgets.common.font import setFont
class TextBrowser(QTextBrowser): # 已弃用
    """ Text edit """

    def __init__(self, parent=None):
        super().__init__(parent=parent)
        self.layer = EditLayer(self)
        self.scrollDelegate = SmoothScrollDelegate(self)
        FluentStyleSheet.LINE_EDIT.apply(self)
        setFont(self)

    def contextMenuEvent(self, e):
        menu = TextEditMenu(self)
        menu.exec(e.globalPos(), ani=True)

class PresetLineEdit(LineEdit):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.textChanged.connect(self._handle_text_changed)
        self._ignore_text_change = False  # 用于防止递归调用的标志

    def _handle_text_changed(self):
        if self._ignore_text_change:
            return
            
        self._ignore_text_change = True
        text = self.text()
        cursor_pos = self.cursorPosition()
        
        # 执行格式化
        formatted = self._format_text(text)
        
        if formatted != text:
            # 计算新的光标位置
            # 当文本变化不大时，尝试保持原光标位置
            if formatted.startswith(text[:cursor_pos]) and text != "":
                new_pos = cursor_pos
            else:
                # 文本变化较大时，将光标移到末尾
                new_pos = len(formatted)
                
            self.setText(formatted)
            self.setCursorPosition(new_pos)
        
        self._ignore_text_change = False

    def _format_text(self, text):
        # 中文逗号转英文
        text = text.replace('，', ',')
        
        # 移除非法字符（只允许数字、逗号和空格）
        text = re.sub(r'[^\d,\s]', '', text)
        
        # 规范化数字间的空格处理
        text = re.sub(r'(\d)\s+(\d)', r'\1\2', text)  # 删除数字之间的空格
        text = re.sub(r',\s*', ', ', text)  # 逗号后统一一个空格
        
        # 移除重复逗号
        text = re.sub(r',+', ',', text)
        
        # 处理首尾空格和逗号
        text = text.strip()
        if text.startswith(','):
            text = text[1:]
        # if text.endswith(','):
        #     text = text[:-1]
            
        # 确保数字后有逗号或者空格处理得当
        text = re.sub(r'(\d),', r'\1,', text)
        
        return text  # 最后再移除一次首尾空格

    def keyPressEvent(self, event):
        # 首先处理所有特殊键和控制键
        key = event.key()
        
        # 处理快捷键（Ctrl+A、Ctrl+C、Ctrl+V等）
        if event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            super().keyPressEvent(event)
            return
            
        # 处理编辑键（退格、删除、方向键等）
        if key in (Qt.Key.Key_Backspace, Qt.Key.Key_Delete, 
                  Qt.Key.Key_Left, Qt.Key.Key_Right,
                  Qt.Key.Key_Home, Qt.Key.Key_End,
                  Qt.Key.Key_Return, Qt.Key.Key_Enter,
                  Qt.Key.Key_Tab, Qt.Key.Key_Escape):
            super().keyPressEvent(event)
            return
            
        # 处理文本输入
        text = event.text()
        
        # 允许数字输入
        if text.isdigit():
            super().keyPressEvent(event)
            return
            
        # 允许逗号输入（包括中文逗号）
        if text in (',', '，'):
            # 总是允许逗号输入，格式化函数会处理中文逗号转换
            super().keyPressEvent(event)
            return
            
        # 允许空格输入
        if text == ' ':
            super().keyPressEvent(event)
            return
            
        # 其他输入忽略
        event.ignore()

def validate_and_format(user_input):
    # 定义允许的动态字段
    allowed_variables = ['self.bot_name', 'self.event_user']

    # 查找 user_input 中的所有动态字段
    start = 0
    result = ""

    while True:
        # 查找下一个 '{'
        start = user_input.find('{', start)
        if start == -1:
            # 找不到 '{'，继续添加剩余部分并结束循环
            result += user_input
            break

        # 查找下一个 '}'
        end = user_input.find('}', start)
        if end == -1:
            # 如果没有找到 '}'，也添加剩余部分并结束循环
            result += user_input[start:]
            break

        # 提取字段
        field = user_input[start + 1:end].strip()  # 取出 '{ }' 中的内容
        full_field = f'{field}'

        print(full_field)
        if f'{full_field}' in allowed_variables:
            # 如果字段在允许列表中，添加对应属性的值
            result += user_input[:start] + '{' + full_field + '}'
        else:
            # 如果字段不被允许，替换为空
            result += user_input[:start]

        # 更新 user_input，继续查找下一个字段
        user_input = user_input[end + 1:]
        start = 0  # 重置起始位置以继续循环

    return result

if __name__ == "__main__":
    a = QApplication()
    p = PresetLineEdit()
    p.show()
    a.exec_()