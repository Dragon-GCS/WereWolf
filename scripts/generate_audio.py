# /// script
# requires-python = ">=3.14"
# dependencies = [
#     "requests>=2.33.1",
# ]
#
# [[tool.uv.index]]
# url = "https://mirrors.aliyun.com/pypi/simple"
# default = true
# ///

import base64
import json
import os
from pathlib import Path

import requests

audios = [
    "天黑请闭眼",
    "天亮了请闭眼",
    "请睁眼",
    "请执行操作",
    "请闭眼",
    "发动技能",
    "玩家出局",
    "阵营胜利",
    "开始投票",
    "发表遗言",
    # 编号
    "一号",
    "二号",
    "三号",
    "四号",
    "五号",
    "六号",
    "七号",
    "八号",
    "九号",
    "十号",
    "十一号",
    "十二号",
    "十三号",
    "十四号",
    "十五号",
    # 角色
    "好人玩家",
    "预言家",
    "女巫",
    "猎人",
    "守卫",
    "灵镜少女",
    "混子",
    "守墓人",
    "魔术师",
    "狼人",
    "狼美人",
    "机械狼",
    "石像鬼",
    "吸血鬼",
    # 警长
    "开始竞选警长",
    "当选警长",
    "移交警徽",
    "选择撕除警徽",
]

URL = "https://openspeech.bytedance.com/api/v3/tts/unidirectional"
API_KEY = os.getenv("ARK_API_KEY")
assert API_KEY, "请设置环境变量 ARK_API_KEY 为你的 API Key"
AUDIO_DIR = Path(__file__).parent.parent / "static" / "audio"
SPEAKER = "zh_male_cixingjieshuonan_uranus_bigtts"

session = requests.Session()
session.headers.update({"X-Api-Key": API_KEY, "X-Api-Resource-Id": "seed-tts-2.0"})


for text in audios:
    output_path = AUDIO_DIR / f"{text}.mp3"
    if output_path.exists():
        print(f"跳过 {text}（已存在）")
        continue

    body = {
        "user": {"uid": "werewolf"},
        "req_params": {
            "text": text,
            "speaker": SPEAKER,
            "audio_params": {"format": "mp3", "sample_rate": 24000},
        },
    }

    response = session.post(URL, json=body, stream=True)
    response.raise_for_status()

    audio_data = bytearray()
    success = False
    for line in response.iter_lines():
        if not line:
            continue
        chunk = json.loads(line)
        code = chunk.get("code", -1)
        if code == 20000000:
            success = True
            break
        if code != 0:
            print(f"错误 [{text}] code={code}: {chunk.get('message')}")
            break
        if chunk.get("data"):
            audio_data.extend(base64.b64decode(chunk["data"]))

    if success and audio_data:
        output_path.write_bytes(audio_data)
        print(f"生成 {text}.mp3")
    else:
        print(f"失败 {text}")
