"""文脉（WenMai）· 文献综述写作与引用自检助手。

设计原则（对应交付要求）：
1. 单命令启动：python -m wenmai demo
2. 断网可演示：本地引擎为主，百炼 API 为增强层，网络异常自动降级
3. 零登录：演示路径不依赖任何需要登录的云控制台
"""

__version__ = "1.0.0"
__app_name__ = "文脉 WenMai"

# 兼容别名：早期版本包名是 `wenmai`，测试脚本与部分文档仍按旧名引用。
# 导入本包后把自身登记为 `wenmai`，`import wenmai.xxx` 即可正常工作——
# 改名（wenmai → 源程序，便于评委一眼认出源码目录）不会连带改坏既有脚本。
import sys as _sys

if __name__ != "wenmai" and "wenmai" not in _sys.modules:
    _sys.modules["wenmai"] = _sys.modules[__name__]
