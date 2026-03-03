import re
import json
from typing import Union


class ObjectedJson:
    def __init__(self, content: Union[dict, list] = None):
        if content is None:
            self.__content = dict()
        else:
            self.__content = content

    def __getattr__(self, attr):
        if attr == "_ObjectedDict__content" or attr == "raw":
            return self.__content
        else:
            try:
                att = self.__content.get(attr)
            except:
                return None
            if isinstance(att, dict):
                return ObjectedJson(att)
            else:
                return att

    def __setattr__(self, attr, value):
        if attr == "_ObjectedJson__content":
            super().__setattr__(attr, value)
        else:
            try:
                self.__content[attr] = value
            except:
                pass

    def __getitem__(self, item):
        if isinstance(self.__content, dict):
            return self.__content.get(item)
        elif isinstance(self.__content, list):
            return self.__content[item]
        else:
            return None

    def __setitem__(self, key, value):
        self.__content[key] = value

    def __iter__(self):
        for i in self.__content:
            yield i

    def __str__(self) -> str:
        return self.__content.__str__()


class Char(str):
    @classmethod
    def convert_from(cls, obj: Union[str, "String"]):
        if len(obj) != 1:
            raise TypeError("Char is not fxxking String!")
        else:
            return cls(obj)

    @property
    def width(self) -> int:
        char = self
        if len(char) != 1:
            raise TypeError("String too long")
        o = ord(char)
        widths = [
            (126, 1), (159, 0), (687, 1), (710, 0), (711, 1),
            (727, 0), (733, 1), (879, 0), (1154, 1), (1161, 0),
            (4347, 1), (4447, 1.75), (7467, 1), (7521, 0), (8369, 1),
            (8426, 0), (9000, 1), (9002, 1.75), (11021, 1), (12350, 1.75),
            (12351, 1), (12438, 1.75), (12442, 0), (19893, 1.75), (19967, 1),
            (55203, 1.75), (63743, 1), (64106, 1.75), (65039, 1), (65059, 0),
            (65131, 1.75), (65279, 1), (65376, 1.75), (65500, 1), (65510, 1.75),
            (120831, 1), (262141, 1.75), (1114109, 1),
        ]
        if o == 0xe or o == 0xf:
            return 0
        for num, wid in widths:
            if o <= num:
                return wid
        return 1


class String(str):
    def cmdl_parse(self) -> list:
        args = []
        temp = ""
        in_sub = False
        last_sig = ""
        for i in (self if self.endswith(" ") else self + " "):
            if in_sub and i == " ":
                temp += i
            elif not in_sub and i == " ":
                args.append(temp)
                temp = ""
            elif i == '"' or i == "'":
                if not in_sub:
                    in_sub = True
                    last_sig = i
                elif i == last_sig:
                    in_sub = False
            else:
                temp += i

        res = []

        for i in args:
            if "=" in i:
                if " " in i:
                    continue
                index = args.index(i)
                new = args[index].split("=", 1)
                args[index] = {new[0]: new[1]}

        for i in args:
            if str(i) != "":
                if isinstance(i, dict):
                    res.append(i)
                else:
                    res.append(String(i))

        del temp, in_sub, last_sig, args

        return res

    def match(self, par: str) -> bool:
        return True if re.match(par, self) else False

    def clear(self) -> "String":
        if len(self) != 0:
            return String("")
        else:
            return self

    def to_json(self) -> Union[list, dict]:
        return json.loads(self)

    def format(self, w_p_l: int = 110) -> "String":
        c_lis = list(map(Char.convert_from, list(self)))
        lines = []
        temp_line = ""
        temp_length = 0
        for i in c_lis:
            if i == "\n":
                lines.append(temp_line)
                temp_line = ""
                temp_length = 0
            temp_line += i
            temp_length += i.width
            if temp_length >= w_p_l:
                lines.append(temp_line)
                temp_line = ""
                temp_length = 0

        lines.append(temp_line)

        return String("\n".join(lines))


class Integer(int):
    @classmethod
    def convert_from(cls, target):
        if isinstance(target, int):
            return cls(target)

        try:
            return cls(int(target))
        except:
            return cls(-1)

