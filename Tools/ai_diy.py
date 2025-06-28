
import json

api_data = {}
current_api = None

def load_api_data(filepath="api_data.json"):#加载api数据
    global api_data, current_api
    try:
        with open(filepath, "r") as f:
            data = json.load(f)
            api_data = data.get("apis", {})
            current_api = data.get("current_api")
    except FileNotFoundError:
        api_data = {}
        current_api = None

def save_api_data(filepath="api_data.json"):
    """将 API 数据保存到 JSON 文件。"""
    with open(filepath, "w") as f:
        json.dump({"apis": api_data, "current_api": current_api}, f, indent=4)


def add_api(url, remark, api_key, model_name):
    load_api_data()
    """添加 AI 接口，包含 API Key 和模型名称。"""
    api_data[remark] = {"url": url, "api_key": api_key, "model_name": model_name}
    save_api_data()
    return f"已添加 API：{remark} - {url} ({model_name}, API Key 已保存)"


def delete_api(remark):
    load_api_data()
    global current_api
    if remark in api_data:
        del api_data[remark]
        if current_api == remark:
            current_api = None
        save_api_data()
        return f"已删除 API：{remark}"
    else:
        return f"未找到 API：{remark}"


def set_api(remark, url=None, api_key=None):
    load_api_data()
    global current_api
    if url is not None: #设置可更新apiurl
        api_data[remark] = {"url": url, "api_key": api_key if api_key is not None else api_data.get(remark, {}).get("api_key")}
        save_api_data()
        return f"已设置 API：{remark} - {url}"
    elif remark in api_data: 
        current_api = remark
        save_api_data()
        return f"已选择 API：{remark}"
    else:
        return f"未找到 API：{remark}"


def list_api():
    load_api_data()
    if not api_data:
        return "没有已保存的 API。"

    result = "已保存的 API：\n"
    for i, (remark, data) in enumerate(api_data.items()):
        result += f"{i+1}. {remark}: {data['url']}"
        if remark == current_api:
            result += " (当前)"
        result += "\n"
    return result


def get_current_api():
    load_api_data()
    global current_api
    if current_api is not None and current_api in api_data:
        data = api_data.get(current_api)
        return data.get("url"), current_api, data.get("api_key"), data.get("model_name")
    else:
        return None, None, None, None
