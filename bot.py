#!/usr/bin/env python3
"""Launcher: подхватывает settings.env и запускает бота.

Значения из settings.env перекрывают переменные окружения (в том числе заданные в workflow),
поэтому настройки можно менять, не трогая .github/workflows. Секреты (TELEGRAM_TOKEN, CHAT_ID)
сюда писать нельзя: они игнорируются и берутся только из секретов GitHub / окружения.
"""
import os
import runpy

PROTECTED = {"TELEGRAM_TOKEN", "CHAT_ID"}


def load_settings(path: str = "settings.env") -> None:
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as f:
        for raw in f:
            line = raw.split("#", 1)[0].strip()
            if not line or "=" not in line:
                continue
            key, value = (p.strip() for p in line.split("=", 1))
            if key and key not in PROTECTED:
                os.environ[key] = value.strip("\"'")


load_settings()
runpy.run_path("ict_bot_improved.py", run_name="__main__")
