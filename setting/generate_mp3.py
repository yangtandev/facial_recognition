
#寫成函式
from gtts import gTTS
from pygame import mixer
import os
import json


path = os.path.join(os.path.dirname(__file__), "../media/pic_bak")
save_path = os.path.dirname(__file__)+"/../voice/"
with open(os.path.join(os.path.dirname(__file__), "../config.json"), "r", encoding="utf-8") as json_file:
    CONFIG = json.load(json_file)

save_name = {}
for file in os.listdir(path):
    name = file.split("_")[-1].split(".")[0]
    if name not in save_name.keys():
        for key in CONFIG["say"].keys():
            txt = CONFIG['say'][key]
            if "name_" in CONFIG['say'][key]:
                txt = txt[5:]
            tts = gTTS(text=f"{name}{txt}", lang='zh-tw')
            tts.save(os.path.join(save_path, f"{name}_{key}.mp3"))

name = ""
for key in CONFIG["say"].keys():
    txt = CONFIG['say'][key]
    if "name_" in CONFIG['say'][key]:
        txt = txt[5:]
    tts = gTTS(text=txt, lang='zh-tw')
    filename = f"{name}_{key}.mp3" if name else f"{key}.mp3"
    tts.save(os.path.join(save_path, filename))
