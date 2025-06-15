from flask import render_template, jsonify, request
from app import app
from app.services.config_service import load_config, save_config

@app.route('/')
def index():
    return render_template(r'index.html')

@app.route('/get_config')
def handle_get_config():
    return jsonify(load_config())

@app.route('/save_config', methods=['POST'])
def handle_save_config():
    try:
        data = request.get_json()
        if not isinstance(data, dict):
            raise ValueError("无效的JSON结构")
            
        save_config(data)
        return jsonify({
            'status': 'success',
            'message': '配置保存成功'
        })
    except Exception as e:
        import traceback
        traceback.print_exc()

        return jsonify({
            'status': 'error',
            'message': f'保存失败: {str(e)}'
        }), 500