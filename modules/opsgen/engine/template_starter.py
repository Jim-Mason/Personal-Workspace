"""Starter YAML for creating custom templates."""

CUSTOM_TEMPLATE_STARTER = """name: my_script
title: 我的运维脚本
description: 简要描述这个模板的用途
icon: 📜
category: general

questions:
  - name: target
    label: 目标路径
    type: string
    default: /tmp/example
    required: true
    help: 向用户展示的提示信息

  - name: enable_debug
    label: 启用调试
    type: bool
    default: false

script: |
  #!/bin/bash
  set -euo pipefail
  TARGET="{{ target }}"
  echo "==> 处理 $TARGET"
  {% if enable_debug %}
  set -x
  {% endif %}
  echo "==> 完成"
"""
