import os
from flask import Flask

base_dir = os.path.abspath(os.path.dirname(__file__))

app = Flask(
    __name__,
    template_folder=os.path.join(base_dir, '../templates'),  # 绝对路径配置
    static_folder=os.path.join(base_dir, '../static')
)

from app import routes  # 确保在实例创建后导入