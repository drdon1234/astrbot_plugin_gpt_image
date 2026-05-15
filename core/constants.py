PLUGIN_NAME = "astrbot_plugin_gpt_image"

COMMAND_NAMES = ["gimg", "生图", "画图"]

PARAMETER_USAGE_MESSAGE = (
    "请选择合法参数（触发词、提示词、分辨率之间用空格分隔）\n"
    "生图 <提示词> <分辨率>（分辨率支持 自动 或 长×宽 / 长*宽 / 长x宽，单边不超过 3840）"
)

IMAGE_MIME_TYPES = {"image/png", "image/jpeg", "image/jpg", "image/webp"}

EXTENSION_BY_MIME = {
    "image/png": "png",
    "image/jpeg": "jpg",
    "image/jpg": "jpg",
    "image/webp": "webp",
}

MAX_REFERENCE_IMAGES = 16
MAX_REFERENCE_MB = 50
MAX_REFERENCE_BYTES = MAX_REFERENCE_MB * 1024 * 1024
IMAGE_DOWNLOAD_TIMEOUT_SECONDS = 30
