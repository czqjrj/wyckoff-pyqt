#!/usr/bin/env python3
"""Wyckoff 回测入口（重定向至 scripts.run）"""
import sys
sys.path.insert(0, '.')
from scripts.run import main
# 不传递参数 - 让 main() 从 sys.argv 中解析参数
main()
