from datetime import datetime, timedelta
yes = ['是', '是的', '对', '有', '在', 'yes', 'y', '1']
no = ['不是', '不', '不对', '否', '没有', '不在', 'no', 'n', '2']
idk = ['我不知道', '不知道', '不清楚', 'idk', '3']
probably = ['可能是', '也许是', '或许是', '应该是', '大概是', '4', 'p']
probablyn = ['可能不是', '也许不是', '或许不是', '应该不是', '大概不是', '5', 'pn']
back = ['返回', '上一个', 'b', 'B']
exit114 = ['exit', '退出']

class GameSession:
    def __init__(self, uid, gid, game_data):
        self.uid = uid
        self.gid = gid
        self.game_data = game_data
        self.timeout = self.timeout = datetime.now() + timedelta(seconds=60)
        self.question_count = 0