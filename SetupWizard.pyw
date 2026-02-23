import json
import subprocess
import traceback
from PySide6.QtCore import Qt, QSize
from PySide6.QtGui import QIcon, QColor
from PySide6.QtWidgets import QApplication, QWidget
import sys, os
import webbrowser

from qfluentwidgets import (SplitFluentWindow, FluentIcon, NavigationAvatarWidget, 
                            NavigationItemPosition, FluentWindow, MSFluentWindow,
                            FluentTranslator, Theme, setTheme, setThemeColor, isDarkTheme, 
                            SplashScreen)
from wizardWindows import JianerSetupWizard_rc
from wizardWindows.Ui_JianerSetupAbout import Ui_Form as Ui_JianerSetupAbout
from wizardWindows.Ui_JianerSetupAdvanced import Ui_Form as Ui_JianerSetupAdvanced
from wizardWindows.Ui_JianerSetupAI import Ui_Form as Ui_JianerSetupAI
from wizardWindows.Ui_JianerSetupApply import Ui_Form as Ui_JianerSetupApply
from wizardWindows.Ui_JianerSetupBasic import Ui_Form as Ui_JianerSetupBasic
from wizardWindows.Ui_JianerSetupLgr import Ui_Form as Ui_JianerSetupLgr
from wizardWindows.Ui_JianerSetupPre import Ui_Form as Ui_JianerSetupPre
from wizardWindows.Ui_JianerSetupWizard import Ui_Form as Ui_JianerSetupWizard

os.chdir(os.path.dirname(os.path.abspath(sys.argv[0])))
config_content = ""
appSettings_content = ""

class JianerSetupWizard(QWidget, Ui_JianerSetupWizard):
    def __init__(self, parent=None):
        super().__init__(parent=parent)
        self.setupUi(self)
        self.SmoothScrollArea.setStyleSheet("background: transparent;")
        
class JianerSetupPre(QWidget, Ui_JianerSetupPre):
    def __init__(self, parent=None):
        super().__init__(parent=parent)
        self.setupUi(self)
        self.ScrollArea.setStyleSheet("background: transparent;")
        
class JianerSetupLgr(QWidget, Ui_JianerSetupLgr):
    def __init__(self, parent=None):
        super().__init__(parent=parent)
        self.setupUi(self)
        self.SmoothScrollArea.setStyleSheet("background: transparent;")
        
class JianerSetupBasic(QWidget, Ui_JianerSetupBasic):
    def __init__(self, parent=None):
        super().__init__(parent=parent)
        self.setupUi(self)
        self.SmoothScrollArea.setStyleSheet("background: transparent;")
        
class JianerSetupApply(QWidget, Ui_JianerSetupApply):
    def __init__(self, parent=None):
        super().__init__(parent=parent)
        self.setupUi(self)
        self.SmoothScrollArea.setStyleSheet("background: transparent;")
        
class JianerSetupAI(QWidget, Ui_JianerSetupAI):
    def __init__(self, parent=None):
        super().__init__(parent=parent)
        self.setupUi(self)
        
class JianerSetupAdvanced(QWidget, Ui_JianerSetupAdvanced):
    def __init__(self, parent=None):
        super().__init__(parent=parent)
        self.setupUi(self)
        self.SmoothScrollArea.setStyleSheet("background: transparent;")
        
class JianerSetupWizard(QWidget, Ui_JianerSetupWizard):
    def __init__(self, parent=None):
        super().__init__(parent=parent)
        self.setupUi(self)
        self.SmoothScrollArea.setStyleSheet("background: transparent;")
        
class JianerSetupAbout(QWidget, Ui_JianerSetupAbout):
    def __init__(self, parent=None):
        super().__init__(parent=parent)
        self.setupUi(self)
        self.SmoothScrollArea.setStyleSheet("background: transparent;")
        
class JianerSetupWizard(QWidget, Ui_JianerSetupWizard):
    def __init__(self, parent=None):
        super().__init__(parent=parent)
        self.setupUi(self)
        self.SmoothScrollArea.setStyleSheet("background: transparent;")

class SetupWizard(SplitFluentWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("简儿 - 设置向导")
        self.setWindowIcon(QIcon(os.path.abspath('.//wizardWindows/Icon_rounded.png')))
        self.resize(1000, 700)
        
        # 1. 创建启动页面
        self.splashScreen = SplashScreen(self.windowIcon(), self)
        self.splashScreen.setIconSize(QSize(128, 128))

        # 2. 在创建其他子页面前先显示主界面
        self.show()

        # 实例化所有页面
        self.jianer_setup_wizard = JianerSetupWizard(self)
        self.jianer_setup_wizard.setObjectName("JianerSetupWizard")
        self.jianer_setup_basic = JianerSetupBasic(self)
        self.jianer_setup_basic.setObjectName("JianerSetupBasic")
        self.jianer_setup_ai = JianerSetupAI(self)
        self.jianer_setup_ai.setObjectName("JianerSetupAI")
        self.jianer_setup_pre = JianerSetupPre(self)
        self.jianer_setup_pre.setObjectName("JianerSetupPre")
        self.jianer_setup_lgr = JianerSetupLgr(self)
        self.jianer_setup_lgr.setObjectName("JianerSetupLgr")
        self.jianer_setup_advanced = JianerSetupAdvanced(self)
        self.jianer_setup_advanced.setObjectName("JianerSetupAdvanced")
        self.jianer_setup_apply = JianerSetupApply(self)
        self.jianer_setup_apply.setObjectName("JianerSetupApply")
        self.jianer_setup_about = JianerSetupAbout(self)
        self.jianer_setup_about.setObjectName("JianerSetupAbout")
        
        # 自适应关于页面的图标颜色
        if isDarkTheme():
            self.jianer_setup_about.IconWidget_2.setIcon(QIcon(os.path.abspath('.//wizardWindows/SimpleIconsBilibili_dark.png')))
            self.jianer_setup_about.IconWidget_4.setIcon(QIcon(os.path.abspath('.//wizardWindows/SimpleIconsGithub_dark.png')))
            self.jianer_setup_about.IconWidget.setIcon(QIcon(os.path.abspath('.//wizardWindows/SimpleIconsQq_dark.png')))
            self.jianer_setup_about.IconWidget_3.setIcon(QIcon(os.path.abspath('.//wizardWindows/SimpleIconsGithub_dark.png')))
        else:
            self.jianer_setup_about.IconWidget_2.setIcon(QIcon(os.path.abspath('.//wizardWindows/SimpleIconsBilibili.png')))
            self.jianer_setup_about.IconWidget_4.setIcon(QIcon(os.path.abspath('.//wizardWindows/SimpleIconsGithub.png')))
            self.jianer_setup_about.IconWidget.setIcon(QIcon(os.path.abspath('.//wizardWindows/SimpleIconsQq.png')))
            self.jianer_setup_about.IconWidget_3.setIcon(QIcon(os.path.abspath('.//wizardWindows/SimpleIconsGithub.png')))
        
        # 定义 jianer_setup_wizard 中所有按钮的点击事件
        self.jianer_setup_wizard.NormalIconButton.clicked.connect(lambda: self.switchTo(self.jianer_setup_basic))
        self.jianer_setup_wizard.NormalIconButton_2.clicked.connect(lambda: self.switchTo(self.jianer_setup_ai))
        self.jianer_setup_wizard.NormalIconButton_4.clicked.connect(lambda: self.switchTo(self.jianer_setup_pre))
        self.jianer_setup_wizard.NormalIconButton_3.clicked.connect(lambda: self.switchTo(self.jianer_setup_lgr))
        self.jianer_setup_wizard.NormalIconButton_5.clicked.connect(lambda: self.switchTo(self.jianer_setup_advanced))
        
        # 定义 jianer_setup_about 中所有按钮的点击事件
        self.jianer_setup_about.NormalIconButton.clicked.connect(lambda: webbrowser.open("https://qm.qq.com/q/fsVJtsZcA2"))
        self.jianer_setup_about.NormalIconButton_2.clicked.connect(lambda: webbrowser.open("https://space.bilibili.com/1969160969"))
        self.jianer_setup_about.NormalIconButton_3.clicked.connect(lambda: webbrowser.open("https://github.com/SRInternet-Studio/Jianer_QQ_bot/"))
        self.jianer_setup_about.NormalIconButton_4.clicked.connect(lambda: webbrowser.open("https://github.com/SRInternet-Studio/Jianer_QQ_bot/issues/new?"))
        self.jianer_setup_about.NormalIconButton_5.clicked.connect(lambda: webbrowser.open("https://www.sr-studio.cn/"))
        
        # 定义 jianer_setup_advanced 中的可选代理
        self.jianer_setup_advanced.SplitPushButton.clear()
        self.jianer_setup_advanced.SplitPushButton.addItem("Master - cloudflare")
        self.jianer_setup_advanced.SplitPushButton.addItem("Master - mainland")
        self.jianer_setup_advanced.SplitPushButton.addItem("Mirror - hk > cloudflare")
        self.jianer_setup_advanced.SplitPushButton.setCurrentIndex(0)
        
        # 定义 jianer_setup_lgr 中的可选日志等级
        self.jianer_setup_lgr.SplitPushButton.clear()
        self.jianer_setup_lgr.SplitPushButton.addItem("DEBUG")
        self.jianer_setup_lgr.SplitPushButton.addItem("TRACE")
        self.jianer_setup_lgr.SplitPushButton.addItem("INFO")
        self.jianer_setup_lgr.SplitPushButton.addItem("WARNING")
        self.jianer_setup_lgr.SplitPushButton.addItem("ERROR")
        self.jianer_setup_lgr.SplitPushButton.addItem("CRITICAL")
        self.jianer_setup_lgr.SplitPushButton.setCurrentIndex(0)

        # 添加子界面
        self.addSubInterface(self.jianer_setup_wizard, QIcon(os.path.abspath('.//wizardWindows/Icon_rounded.png')), "欢迎")
        self.addSubInterface(self.jianer_setup_basic, FluentIcon.ALIGNMENT, "基本信息设置")
        self.addSubInterface(self.jianer_setup_ai, FluentIcon.TRANSPARENT, "AI 设置")
        self.addSubInterface(self.jianer_setup_pre, FluentIcon.PENCIL_INK, "预设")
        self.addSubInterface(self.jianer_setup_lgr, FluentIcon.DEVELOPER_TOOLS, "高级设置")
        self.addSubInterface(self.jianer_setup_advanced, FluentIcon.APPLICATION, "框架设置")
        self.addSubInterface(self.jianer_setup_apply, FluentIcon.SAVE_AS, "核对并应用设置")
        self.addSubInterface(self.jianer_setup_about, FluentIcon.INFO, "关于", NavigationItemPosition.BOTTOM)
        
        self.stackedWidget.currentChanged.connect(self.update_plain_text)
        self.jianer_setup_apply.PrimaryPushButton.clicked.connect(self.save_settings)
        self.jianer_setup_apply.PrimaryPushButton_2.clicked.connect(lambda: subprocess.Popen(['python', 'main.py']))
        
        self.read_settings()
        self.splashScreen.finish()
        
    def update_plain_text(self):
        if self.stackedWidget.currentWidget() == self.jianer_setup_apply:
            self.jianer_setup_apply.LargeTitleLabel.setText("核对设置清单并生成设置")
            self.jianer_setup_apply.LargeTitleLabel.setTextColor()
            
            config_text = f'''——————QQ机器人 配置设置——————
    {GenerateSettings.gen_config(GenerateSettings, 
            self.jianer_setup_basic.LineEdit_2.text(), 
            self.jianer_setup_lgr.LineEdit.text(), 
            self.jianer_setup_lgr.LineEdit_6.text(), 
            self.jianer_setup_ai.LineEdit_6.text(), 
            self.jianer_setup_ai.LineEdit.text(), 
            self.jianer_setup_basic.LineEdit.text(), 
            self.jianer_setup_basic.LineEdit_6.text(),   
            self.jianer_setup_basic.LineEdit_2.text(), 
            self.jianer_setup_basic.PlainTextEdit.toPlainText(), 
            self.jianer_setup_basic.LineEdit_5.text(), 
            self.jianer_setup_lgr.SplitPushButton.currentText(), 
            self.jianer_setup_lgr.PlainTextEdit.toPlainText())}'''
            
            app_settings_text = f'''——————框架设置——————
    {GenerateSettings.gen_appSettings(GenerateSettings, 
            self.jianer_setup_basic.LineEdit_4.text(), 
            self.jianer_setup_lgr.LineEdit.text(), 
            self.jianer_setup_lgr.LineEdit_6.text(), 
            self.jianer_setup_advanced.SplitPushButton.currentText())}'''

            self.jianer_setup_apply.PlainTextEdit.setPlainText(config_text + '\n\n' + app_settings_text)
        elif self.stackedWidget.currentWidget() == self.jianer_setup_about:
            self.jianer_setup_about.SubtitleLabel_7.setText(f"请在QQ群中发送消息 {self.jianer_setup_basic.LineEdit_5.text()}关于 来查看更详细的关于信息。")
            
    def read_settings(self):
        
        if os.path.isfile(".\\config.json"):
            with open(".\\config.json", "r", encoding="utf-8") as f:
                settings = f.read()
                f.close()
                
                data = json.loads(settings)
                connection = data["Connection"]
                others = data["Others"]

                self.jianer_setup_basic.LineEdit_2.setText("\n".join(str(item) for item in data["owner"]))
                self.jianer_setup_lgr.LineEdit.setText(str(connection["host"]))
                self.jianer_setup_lgr.LineEdit_6.setText(str(connection["port"]))
                self.jianer_setup_ai.LineEdit.setText(str(others["gemini_key"]))
                self.jianer_setup_ai.LineEdit_6.setText(str(others["openai_key"]))
                self.jianer_setup_basic.LineEdit.setText(str(others["bot_name"]))
                self.jianer_setup_basic.LineEdit_6.setText(str(others["bot_name_en"]))
                self.jianer_setup_basic.LineEdit_2.setText("\n".join(str(item) for item in others["ROOT_User"]))
                self.jianer_setup_basic.PlainTextEdit.setPlainText("\n".join(str(item) for item in others["Auto_approval"]))
                self.jianer_setup_basic.LineEdit_5.setText(str(others["reminder"]))
                self.jianer_setup_lgr.SplitPushButton.setCurrentText(str(data["Log_level"]))
                self.jianer_setup_lgr.PlainTextEdit.setPlainText("\n".join(str(item) for item in data["black_list"]))
                
        if os.path.isfile(".\\appsettings.json"):
            with open(".\\appsettings.json", "r", encoding="utf-8") as f:
                settings = f.read()
                f.close()
                
                data = json.loads(settings)
                Account = data["Account"]
                print(Account)
                Implementations = data["Implementations"][0]
                
                self.jianer_setup_basic.LineEdit_4.setText(str(Account["Uin"]))
                self.jianer_setup_lgr.LineEdit.setText(str(Implementations["Host"]))
                self.jianer_setup_lgr.LineEdit_6.setText(str(Implementations["Port"]))
                
                print(str(data["SignServerUrl"]))
                match str(data["SignServerUrl"]):
                    case "https://sign.lagrangecore.org/api/sign/30366":
                        self.jianer_setup_advanced.SplitPushButton.setCurrentIndex(0)
                    case "http://106.54.14.24:8084/api/sign/30366":
                        self.jianer_setup_advanced.SplitPushButton.setCurrentIndex(1)
                    case "https://sign.0w0.ing/api/sign/30366":
                        self.jianer_setup_advanced.SplitPushButton.setCurrentIndex(2)
                    case _:
                        pass
            
    def save_settings(self):
        global config_content, appSettings_content
        
        GenerateSettings.gen_config(GenerateSettings, 
            self.jianer_setup_basic.LineEdit_2.text(), 
            self.jianer_setup_lgr.LineEdit.text(), 
            self.jianer_setup_lgr.LineEdit_6.text(), 
            self.jianer_setup_ai.LineEdit_6.text(), 
            self.jianer_setup_ai.LineEdit.text(), 
            self.jianer_setup_basic.LineEdit.text(), 
            self.jianer_setup_basic.LineEdit_6.text(),   
            self.jianer_setup_basic.LineEdit_2.text(), 
            self.jianer_setup_basic.PlainTextEdit.toPlainText(), 
            self.jianer_setup_basic.LineEdit_5.text(), 
            self.jianer_setup_lgr.SplitPushButton.currentText(), 
            self.jianer_setup_lgr.PlainTextEdit.toPlainText())
        
        GenerateSettings.gen_appSettings(GenerateSettings, 
            self.jianer_setup_basic.LineEdit_4.text(), 
            self.jianer_setup_lgr.LineEdit.text(), 
            self.jianer_setup_lgr.LineEdit_6.text(), 
            self.jianer_setup_advanced.SplitPushButton.currentText())
        
        prere = GenerateSettings.prerequisites(GenerateSettings, 
                                               self.jianer_setup_pre.PlainTextEdit.toPlainText(), 
                                               self.jianer_setup_pre.PlainTextEdit_2.toPlainText(), 
                                               self.jianer_setup_pre.PlainTextEdit_3.toPlainText())
        
        if config_content is None or appSettings_content is None:
            self.jianer_setup_apply.LargeTitleLabel.setText("设置的配置有误，请核对设置清单")
            self.jianer_setup_apply.LargeTitleLabel.setTextColor(QColor(255, 60, 48), QColor(255, 60, 48))
        else:
            try:
                with open(".\\config.json", "w", encoding="utf-8") as f:
                    f.write(config_content)
                    f.close()
                
                with open(".\\appsettings.json", "w", encoding="utf-8") as f:
                    f.write(appSettings_content)
                    f.close()
                    
                with open(".\\prerequisites.py", "w", encoding="utf-8") as f:
                    f.write(prere)
                    f.close()
                
                self.jianer_setup_apply.LargeTitleLabel.setText("已成功保存")
                self.jianer_setup_apply.LargeTitleLabel.setTextColor(QColor(82, 210, 97), QColor(82, 210, 97))
            except Exception as e:
                traceback.print_exc()
                self.jianer_setup_apply.LargeTitleLabel.setText("无法写入文件，请检查权限")
                self.jianer_setup_apply.LargeTitleLabel.setTextColor(QColor(255, 60, 48), QColor(255, 60, 48))

        
class GenerateSettings():
    global appSettings_content, config_content
        
    def gen_config(self, uin: str, host: str, port: str, gemini: str, openai: str, 
               botName: str, botNameEN: str, ROOT_User, Auto_approval: str, 
               reminder: str, Log_level: str, black_list: str) -> str:
        
        global config_content
        
        try:
           # 将每一行分割成列表，使用列表推导式去掉每一行可能的空白开头和结尾
            ROOT_User = ROOT_User.splitlines()
            ROOT_User_list = [line.strip() for line in ROOT_User if line.strip()]
            
            uin = uin.splitlines()
            uin_list = [line.strip() for line in uin if line.strip()]
            
            Auto_approval = Auto_approval.splitlines()
            Auto_approval_list = [line.strip() for line in Auto_approval if line.strip()]
            
            black_list = black_list.splitlines()
            black_list_list = [line.strip() for line in black_list if line.strip()]
            
            print(uin_list)
            
            config_content = self.config(self, uin=uin_list, host=host, port=int(port), gemini=gemini, openai=openai, 
                botName=botName, botNameEN=botNameEN, ROOT_User=ROOT_User_list, Auto_approval=Auto_approval_list, 
                reminder=reminder, Log_level=Log_level, black_list=black_list_list)
            config_content = json.dumps(config_content, indent=4, ensure_ascii=False)
            
            return config_content 
        except Exception as e:
            config_content = None
            return f"设置的配置有误，请核对设置清单：{str(traceback.format_exc())}"
    
    def gen_appSettings(self, uin: str, host: str, port: str, proxy: str) -> str:
        
        global appSettings_content
        
        try:
            match proxy:
                case "Master - cloudflare":
                    proxy = "https://sign.lagrangecore.org/api/sign/30366"
                case "Master - mainland":
                    proxy = "http://106.54.14.24:8084/api/sign/30366"
                case "Mirror - hk > cloudflare":
                    proxy = "https://sign.0w0.ing/api/sign/30366"
                case _:
                    proxy = ""
            
            appSettings_content = self.appSettings(uin=int(uin), proxy=proxy, host=host, port=int(port))
            appSettings_content = json.dumps(appSettings_content, indent=4, ensure_ascii=False)
            
            return appSettings_content
        except Exception as e:
            appSettings_content = None
            return f"设置的配置有误，请核对设置清单：{str(traceback.format_exc())}"
        

    def config(self, uin: list = None, host: str = "127.0.0.1", port: int = 5004, gemini: str = "", openai: str = "", 
           botName: str = "简儿", botNameEN: str = "Jianer", ROOT_User: list = None, Auto_approval: list = None, 
           reminder: str = "/", Log_level: str = "INFO", black_list: list = None) -> dict:

        if uin is None:
            uin = []
        if ROOT_User is None:
            ROOT_User = []
        if Auto_approval is None:
            Auto_approval = []
        if black_list is None:
            black_list = []

        print(uin)
        return {
    "owner": uin,
    "black_list": black_list,
    "silents": [
    ],
    "Connection": {
      "mode": "FWS",
      "host": host,
      "port": port,
      "listener_host": host,
      "listener_port": 5003,
      "retries": 5,
      "satori_token": ""
    },
    "Log_level": Log_level,
    "protocol": "OneBot",
    "Others": {
      "gemini_key": gemini,
      "openai_key": openai,
      "bot_name": botName ,
      "bot_name_en": botNameEN,
      "ROOT_User": ROOT_User,
      "Auto_approval": Auto_approval,
      "reminder": reminder
      },
      "uin": 0
}
    
    def appSettings(uin: int, proxy: str, host: str, port: int):
        return {
    "$schema": "https://raw.githubusercontent.com/LagrangeDev/Lagrange.Core/master/Lagrange.OneBot/Resources/appsettings_schema.json",
    "Logging": {
        "LogLevel": {
            "Default": "Information",
            "Microsoft": "Warning",
            "Microsoft.Hosting.Lifetime": "Information"
        }
    },
    "SignServerUrl": proxy,
    "SignProxyUrl": "",
    "MusicSignServerUrl": "",
    "Account": {
        "Uin": uin,
        "Protocol": "Linux",
        "AutoReconnect": True,
        "GetOptimumServer": True
    },
    "Message": {
        "IgnoreSelf": True,
        "StringPost": False
    },
    "QrCode": {
        "ConsoleCompatibilityMode": False
    },
    "Implementations": [
        {
            "Type": "ForwardWebSocket",
            "Host": host,
            "Port": port,
            "HeartBeatInterval": 5000,
            "AccessToken": ""
        }
    ]
}
        
        
    def prerequisites(self, n, s, j):
        girl_friend = self.validate_and_format(self, n)
        sister = self.validate_and_format(self, s)
        Ju_Heqiu = self.validate_and_format(self, j)
        
        return f'''class prerequisite:
    def __init__(self, bot_name: str, event_user: str):
        self.bot_name = bot_name
        self.event_user = event_user
        
    def mother(self) -> str:
        return f"""{Ju_Heqiu}"""

    def girl_friend(self) -> str:
        return f"""{girl_friend}"""

    def sister(self) -> str:
        return f"""{sister}"""
        
'''

    def validate_and_format(self, user_input):
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
        
if __name__ == '__main__':
    QApplication.setHighDpiScaleFactorRoundingPolicy(Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)
    QApplication.setAttribute(Qt.ApplicationAttribute.AA_EnableHighDpiScaling)
    QApplication.setAttribute(Qt.ApplicationAttribute.AA_UseHighDpiPixmaps)
    
    # 设置跟随系统主题和主题色
    setTheme(Theme.AUTO)
    setThemeColor("#e9adb0")
    
    app = QApplication(sys.argv)
    
    t = FluentTranslator()
    app.installTranslator(t)
    
    w = SetupWizard()
    w.show()
    app.exec()    
    