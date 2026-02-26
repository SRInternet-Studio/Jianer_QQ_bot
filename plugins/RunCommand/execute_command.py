def execute_command(command, subprocess, timeout=30, encoding='utf-8', errors='ignore', 
                    shell=False, input_data=None, environment=None):
    """
    执行系统命令，支持超时控制、自定义编码、环境变量等
    
    参数:
    command      - 要执行的命令（字符串或列表）
    subprocess   - subprocess 模块
    timeout      - 命令执行超时时间（秒），默认30秒
    encoding     - 输出编码，默认'utf-8'
    errors       - 解码错误处理方式，默认'ignore'
    shell        - 是否使用shell执行，默认False（推荐）
    input_data   - 输入到命令的数据（字符串或字节）
    environment  - 自定义环境变量（字典）
    
    返回:
    包含执行结果的字典
    """
    # 定义安全的解码函数
    def safe_decode(data, enc=encoding, err=errors):
        """安全解码字节数据"""
        if data is None:
            return ""
        try:
            return data.decode(enc, errors=err)
        except UnicodeDecodeError:
            # 使用更宽松的错误处理策略
            return data.decode(enc, errors="replace")
        except Exception:
            # 在无法解码的情况下返回原始数据
            return str(data)

    # 参数验证
    if not isinstance(command, (list, tuple, str)):
        return {
            "stdout": None,
            "stderr": "Error: command must be a string or list of strings",
            "returncode": -2
        }
    
    import platform
    is_windows = platform.system() == 'Windows'
    is_server = is_windows and 'Server' in platform.release()
    
    # 准备命令
    use_shell = shell or is_windows  # Windows下默认使用shell
    
    if isinstance(command, str):
        if not use_shell:
            try:
                # Linux下尝试分割命令字符串
                import shlex
                command = shlex.split(command)
            except Exception:
                use_shell = True
        elif is_windows:
            # Windows内置命令列表
            windows_builtins = {
                'echo', 'dir', 'copy', 'del', 'erase', 'ren', 'rename',
                'type', 'cd', 'md', 'mkdir', 'rd', 'rmdir', 'cls', 'date',
                'time', 'ver', 'vol', 'set', 'start', 'pause', 'exit'
            }
            
            first_word = command.strip().split()[0].lower()
            if first_word in windows_builtins: # 内置加cmd /c前缀
                command = f'cmd /c {command}'
            
            # Windows Server
            if is_server:
                import os
                system_root = os.environ.get('SystemRoot', r'C:\Windows')
                system32 = os.path.join(system_root, 'System32')
                if system32 not in os.environ['PATH']:
                    os.environ['PATH'] = f"{system32};{os.environ['PATH']}"
    
    try:
        # 构建执行参数
        params = {
            "args": command,
            "stdout": subprocess.PIPE,
            "stderr": subprocess.PIPE,
            "text": False,  # 保持原始字节流，我们自己处理编码
            "timeout": timeout,
            "shell": use_shell
        }
        
        # 添加可选参数
        if input_data is not None:
            # 如果输入是字符串，转换为字节
            if isinstance(input_data, str):
                input_data = input_data.encode(encoding)
            params["input"] = input_data
        
        if environment is not None:
            # 复制当前环境并更新
            import os
            env = os.environ.copy()
            env.update(environment)
            params["env"] = env
        
        # 执行命令
        result = subprocess.run(**params)
        
        # 解码输出
        stdout = safe_decode(result.stdout)
        stderr = safe_decode(result.stderr)
        
        return {
            "stdout": stdout,
            "stderr": stderr,
            "returncode": result.returncode
        }

    except subprocess.CalledProcessError as e:
        return {
            "stdout": safe_decode(e.stdout),
            "stderr": safe_decode(e.stderr) if e.stderr is not None else str(e),
            "returncode": e.returncode
        }
    except subprocess.TimeoutExpired as e:
        # 特殊处理超时情况
        stdout = safe_decode(e.stdout) if e.stdout is not None else ""
        stderr = safe_decode(e.stderr) if e.stderr is not None else ""
        stderr += f"\nCommand timed out after {timeout} seconds"
        
        return {
            "stdout": stdout,
            "stderr": stderr,
            "returncode": -3,
            "timeout": True
        }
    except FileNotFoundError as e:
        cmd_name = command[0] if isinstance(command, list) else command.split()[0]
        return {
            "stdout": None,
            "stderr": f"Command not found: {cmd_name}\nError: {str(e)}",
            "returncode": -4
        }
    except PermissionError as e:
        cmd_name = command[0] if isinstance(command, list) else command.split()[0]
        return {
            "stdout": None,
            "stderr": f"Permission denied for command: {cmd_name}\nError: {str(e)}",
            "returncode": -5
        }
    except KeyboardInterrupt:
        return {
            "stdout": None,
            "stderr": "Command execution interrupted by user",
            "returncode": -6
        }
    except Exception as e:
        return {
            "stdout": None,
            "stderr": f"Unexpected error: {type(e).__name__}\nDetails: {str(e)}",
            "returncode": -1
        }