"""Shared generation defaults from docs/common_parameter_settings.txt."""

import os

BG_VIDEO_OPACITY = 10
BG_VIDEO_MIN_SECONDS = 20.0
MUSIC_VOLUME = 10
MUSIC_MIN_SECONDS = 20.0
SPLIT_AUDIO = True
SPLIT_AUDIO_CHUNKS = 4
SPLIT_AUDIO_SPREAD = 10
SUBLIMINAL_TARGETS = ("Subheading",)
SUBLIMINAL_K = 7
SUBLIMINAL_HIDE_PCT = 55
SUBLIMINAL_ALL_INTRA = False
ENCODER_PRESET = "veryfast"
HASHTAG_SOURCE = "none"
UPLOAD_PLATFORMS = "yt"
MAIL_TO = "founder@quislab.com"
DRIVE_FOLDER = "https://drive.google.com/drive/folders/1cUAGsM-EZh1EJIcH3GBaruWPI7lnSChU"
MATERIAL_FOLDERS = {
    "gifs_drive": "https://drive.google.com/drive/folders/1tCEeNU-CahLwu71VOPBztoUB1HdbVMbt",
    "bg_images_drive": "https://drive.google.com/drive/folders/1_9dJDCgnYkSfZ_OZu6W2OLj7fTSZ5Bad",
    "bg_videos_drive": "https://drive.google.com/drive/folders/1uxgshH4u-D403vXDIzfBqzgYFnhVkbFK",
    "music_drive": "https://drive.google.com/drive/folders/1wfO3ji64J0ubz6O7jRQtePN-Vk4WK5h2",
}


def max_workers() -> int:
    return max(4, os.cpu_count() or 4)
