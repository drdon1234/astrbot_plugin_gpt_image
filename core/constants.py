PLUGIN_NAME = "astrbot_plugin_gpt_image"

STATUS_COMMAND_NAMES = ["生图额度"]

PARAMETER_USAGE_MESSAGE = (
    "请选择合法参数（触发词后、末尾分辨率前的全部内容都会作为提示词）\n"
    "触发词 <提示词> <可选分辨率>（分辨率支持 宽x高 或 比例/方向 + 几K）"
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
