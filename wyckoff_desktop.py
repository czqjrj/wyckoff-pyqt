#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Wyckoff 分析客户端 (PyQt6 版) 入口。

用法:
    conda run -n wyckoff-pyqt python wyckoff_desktop.py
"""
import sys


def main():
    from desktop.main_window import main
    main()


if __name__ == "__main__":
    sys.exit(main())
