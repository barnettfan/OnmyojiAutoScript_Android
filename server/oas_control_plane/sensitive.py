import re

# from module.base.mask import Mask
# from module.ui.page import *
#
# MASK_MAIN = Mask('./assets/mask/MASK_MAIN.png')
# MASK_PLAYER = Mask('./assets/mask/MASK_PLAYER.png')


def handle_sensitive_image(image):
    """
    Args:
        image:

    Returns:
        np.ndarray:
    """
    # if PLAYER_CHECK.match(image, offset=(30, 30)):
    #     image = MASK_PLAYER.apply(image)
    # if MAIN_CHECK.match(image, offset=(30, 30)):
    #     image = MASK_MAIN.apply(image)

    return image


def handle_sensitive_text(text):
    """
    Args:
        text (str):

    Returns:
        str:
    """
    # 兼容 OAS 从 Alas 演进而来的历史日志格式，上传错误现场时不能暴露
    # Agent 容器中的真实工作路径。
    # 长名称置前，避免正则在 ``OnmyojiAutoScript-Self`` 的短名称处提前结束。
    project_pattern = r'(?:OnmyojiAutoScript-Self|AzurLaneAutoScript|OnmyojiAutoScript)'
    text = re.sub(
        rf'File \"(.*?)(?P<project>{project_pattern})',
        'File \"C:\\\\fakepath\\\\\\g<project>',
        text,
    )
    text = re.sub(
        rf'\[Adb_binary\] (.*?)(?P<project>{project_pattern})',
        '[Adb_binary] C:\\\\fakepath\\\\\\g<project>',
        text,
    )
    # 错误现场中的损坏 JSON、异常栈或第三方库输出不一定符合上述两种历史
    # 日志格式。只要发现带有项目根目录的 POSIX/Windows 路径，也统一替换，
    # 避免上传诊断资料时泄露 Agent 容器或宿主机目录。
    text = re.sub(
        rf'(?:(?:[A-Za-z]:)?(?:[/\\][^/\\\s"\']+)+[/\\])(?P<project>{project_pattern})',
        'C:\\\\fakepath\\\\\\g<project>',
        text,
    )
    return text


def handle_sensitive_logs(logs):
    return [handle_sensitive_text(line) for line in logs]
