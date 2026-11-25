"""最小化 Config（仅 browser_register.py 用到的字段）。

剥离自原 CTF-reg/config.py，去掉 card / billing / stripe / captcha 等支付相关字段，
仅保留注册阶段必需的 proxy 字段。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Optional


@dataclass
class Config:
    """ChatGPT 注册最小配置。"""
    # 出口代理 URL，例：socks5://user:pass@host:port  或  socks5://127.0.0.1:18899
    # 留 None 走系统直连
    proxy: Optional[str] = None

    # 换出口 IP 的回调（可选）。warmup 拿不到 oai-did 时调用它换一条新 IP，
    # 而不是拿同一个出口反复重试 —— 实测同一 sid 的出口 IP 是完全粘的，
    # 原样重试只是在等同一个坏 IP。
    # 返回新代理 URL；返回 None 表示当前拿不到新 IP，调用方沿用旧值。
    proxy_rotate: Optional[Callable[[], Optional[str]]] = None
