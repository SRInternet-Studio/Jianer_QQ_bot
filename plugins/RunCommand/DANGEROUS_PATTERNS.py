DANGEROUS_PATTERNS = [
     # === Linux/macOS 删除 ===
    r"\b(rm|unlink)\s+.*(-[rf]+|--recursive|--force|--no-preserve-root)\s+[\"\']?/",  # 递归强制删除
    r"\bfind\s+.*(-delete|-exec\s+rm\s+.*\;|\s*\|.*xargs\s+rm)",                     # 通过find删除
    r"\b(shred|wipe)\s+.*(-u|--remove)\s+.*(/etc|/home|/var)",                      # 安全删除工具
    
    # 针对 shred 的专项检测
    r"shred\s+.*(-u\s+-z|--remove\s+--zero)\s+.*\.(exe|dll|sys|ini|conf|db)",
    r"shred\s+.*--iterations=\d+\s+.*-v\s+.*(\.\w+|/[\w\.]+)",
    
    # === Windows 删除 ===
    r"\b(del|erase|rd|rmdir)\s+.*(/s\s+.*/q|/q\s+.*/s)\s+.*(\\|C:)",                # 静默递归删除
    r"\bRemove-Item\s+.*-Recurse\s+.*-Force\s+.*(C:\\|\\\\)",                       # PowerShell删除
    r"\bTakeown\s+.*/F\s+.*\s+&&\s+.*icacls\s+.*/grant\s+.*:F",                     # 夺取权限后删除
    
    # === 跨平台删除 ===
    r"\b(mv|move|rename)\s+.*(/dev/null|NUL)\s+.*\.(conf|db|log)",                  # 移动伪删除
    r"\b(>|>>)\s+.*(/etc|/var|C:\\Windows)",                                        # 清空文件内容
    r"\b(truncate|:>)\s+.*\.(bash_history|mysql_history)",                          # 截断历史文件
    
    # === 存储设备擦除 ===
    r"\b(dd|diskutil)\s+.*(zero|random|erase)\s+.*(/dev/sd|/dev/disk)",             # 低级擦除
    r"\bFormat-Volume\s+.*-FileSystem\s+NTFS\s+.*-Force\s+.*-DriveLetter\s+C",      # PowerShell格式化
    
    # === 文件系统破坏类 ===
    r"rm\s+(-rf|-\w*rf|\s*--no-preserve-root)\s+[\"\']?/",  # Linux/macOS 根目录删除
    r"dd\s+.*(of=/dev/sd|/dev/disk|/dev/mem)",             # 磁盘/内存覆写
    r"ch(mod|own)\s+.*(-R|--recursive)\s+.*/",             # 递归权限修改
    r"(del|erase)\s+.*/s\s+.*/q\s+.*(\\|C:)",              # Windows 静默删除
    r"format\s+.*/FS:\s*\w+\s+.*/y\s+.*(C:|\\\\)",         # Windows 强制格式化
    r"(echo|printf)\s+.*[a-zA-Z0-9+/=]{20,}\s*\|.*base64\s+-d\s*\|.*sh",
    r"\\x72\\x6d\\x20\\x72\\x66\\x20\\x2f",  # rm -rf / 的十六进制形式
    
    # === 系统控制类 ===
    r"shutdown\s+.*(-h\s+now|/s\s+.*/t\s+0)",              # 立即关机（跨平台）
    r"(halt|poweroff|reboot)\s+.*-f",                      # 强制关机/重启
    r":\(\)\{.*:\|.*&\s*\}.*;",                            # Fork炸弹（Linux/macOS）
    r"bcdedit\s+.*/(set|deletevalue)\s+.*(testsigning|bootstatuspolicy)",  # Windows 启动项篡改
    
    # === 进程与权限提升类 ===
    r"(kill|pkill)\s+.*-9\s+.*(-1|\d+)",                   # 强制终止进程
    r"\b(cmd\s+/c\s+)?(taskkill|tskill)\s+.*(/F|/f)\s+.*(/IM|/im)\s+.*(wininit\.exe|csrss\.exe|lsass\.exe|smss\.exe|winlogon\.exe|services\.exe|spoolsv\.exe|explorer\.exe|\*)",  # Windows 关键进程终止
    r"\b(cmd\s+/c\s+)?(taskkill|tskill)\s+.*(/F|/f)\s+.*(/PID|/pid)\s+.*\d+",  # Windows 按PID终止进程
    r"sudo\s+.*(chmod|chown|visudo|passwd)\s+.*(777|root)", # 危险sudo操作
    r"netsh\s+.*(advfirewall\s+set\s+allprofiles\s+state\s+off|add\s+rule)", # Windows 防火墙操作
    r"/(etc|usr|bin|lib|root|var|home|boot|dev)(/|\s|$)", # 路径特征检测
    r"[A-Z]:\\(Windows|Program\s*Files|Users|System32)(\\|$)",
    r"\\.*\.(sys|dll|exe|so|dylib|conf|cfg|db)\s*$"
    
    # === 网络与远程控制类 ===
    r"(curl|wget)\s+.*(\.sh|\.py|\.exe)\s*(\||\$)",  # 下载可执行文件
    r"curl\s+.*-X\s+PUT\s+.*@",                      # 文件上传
    r"wget\s+.*--post-file\s+",                      # 文件上传
    r"(curl|wget)\s+.*file:///(etc|root|C:)",        # 本地文件泄露
    r"ssh\s+.*-o\s+StrictHostKeyChecking=no\s+.*(&&|\|\|)",   # 非交互式SSH连接
    r"nc\s+.*(-e\s+/bin/sh|-lvp\s+\d+)",                     # 反向Shell
    r"powershell\s+.*(Invoke-Expression|IEX)\s+.*http",      # PS远程代码执行
    
    # === 用户与认证类 ===
    r"(user|group)(add|del|mod)\s+.*(root|admin|0)",        # 系统用户操作
    r"passwd\s+.*(-d|--delete)\s+.*root",                   # 删除root密码
    r"net\s+.*(user|group)\s+.*(/add|/del)\s+.*admin",      # Windows用户操作
    
    # === 敏感数据访问类 ===
    r"(cat|more|less|type)\s+.*(/etc/shadow|/proc/self/mem|C:\\Windows\\System32\\config\\SAM)", # 敏感文件读取
    r"grep\s+.*(-i\s+)?(password|pwd|secret)\s+.*\.(conf|ini|xml)", # 配置文件扫描
    
    # === 特殊符号注入类 ===
    r"[;&|]\s*$",                                          # 命令拼接符号
    r"\$\(.*\)",                                           # 子命令注入
    r"\{.*\}",                                             # 花括号扩展
    r"`.*`",                                               # 反引号执行
    
    # === 虚拟化/容器逃逸类 ===
    r"docker\s+.*(--privileged|/var/run/docker.sock)",     # Docker特权操作
    r"vmrun\s+.*(deleteVM|runProgramInGuest)",             # 虚拟机危险操作
    
    # === Windows特定危险命令 ===
    r"reg\s+.*(add|delete)\s+.*HKLM",                      # 注册表关键操作
    r"schtasks\s+.*/create\s+.*/tn\s+.*/tr\s+.*cmd",       # 计划任务创建
    r"vssadmin\s+.*delete\s+.*shadows",                    # 删除卷影副本
    
    # === macOS特定危险命令 ===
    r"csrutil\s+.*disable",                                # 关闭SIP保护
    r"spctl\s+.*--master-disable",                         # 禁用Gatekeeper
    
    # === 新增Windows危险命令 ===
    r"\b(cmd\s+/c\s+)?(attrib)\s+.*(\+h\s+.*\+s|\+s\s+.*\+h)\s+.*(C:\\|\\\\)",  # 隐藏系统文件
    r"\b(cmd\s+/c\s+)?(fsutil)\s+.*(behavior|usn|dirty)\s+.*(query|set)",       # 文件系统工具滥用
    r"\b(cmd\s+/c\s+)?(wmic)\s+.*(process|service)\s+.*(delete|call)",          # WMI危险操作
    r"\b(cmd\s+/c\s+)?(sc)\s+.*(config|delete)\s+.*(winmgmt|wscsvc|w32time)",  # 服务配置修改
    
    # === 新增Linux危险命令 ===
    r"\b(chattr|lsattr)\s+.*(\+i|\+a)\s+.*(/etc|/bin|/sbin)",                   # 文件属性修改
    r"\b(umount)\s+.*(-l|--lazy)\s+.*(/dev/sd|/boot|/root)",                    # 强制卸载
    r"\b(ldconfig)\s+.*(/etc|/lib|/usr/lib)",                                  # 动态链接库配置
    r"\b(swapoff|swapon)\s+.*(-a|/dev/)",                                      # 交换分区操作
    
    # === 跨平台危险命令 ===
    r"\b(nc|netcat|socat)\s+.*(-e\s+/bin/sh|-c\s+.*sh)",                       # 反向Shell
    r"\b(ssh)\s+.*(-R\s+\d+:|\\-\\-remote\\-forward)",                         # SSH端口转发
    r"\b(python|perl|ruby)\s+.*-c\s+.*(import\s+os|system\()",                 # 脚本执行系统命令
    
    # === 隐蔽执行技术 ===
    r"\b(base64|xxd)\s+.*-d\s*\|.*sh",                                        # Base64解码执行
    r"\b(eval|exec)\s+.*(\\$\(|`|\{)",                                        # 动态执行
    r"\b(echo|printf)\s+.*\\x[0-9a-f]{2}.*\|.*sh",                           # 十六进制编码执行
]