# OpsGen - 运维脚本生成器

通过问答式 Web 交互，自动生成可直接使用的运维脚本（部署、诊断、备份、清理等）。

## 技术栈

- **后端**: Python + Flask
- **模板引擎**: Jinja2
- **前端**: HTML5 + CSS3 + 原生 JavaScript
- **配置解析**: PyYAML
- **代码高亮**: highlight.js
- **实时执行**: WebSocket (flask-socketio)
- **存储**: 文件系统（无数据库）

## 快速开始

```bash
# 安装依赖
pip install -r requirements.txt

# 启动服务
python app.py

# 访问 http://localhost:5000
```

## 内置模板（51 个）

| 分类 | 模板 |
|------|------|
| **deploy 部署** | Nginx 安装、Docker 部署、服务器初始化、Redis、PostgreSQL 安装、Nginx 反向代理、Systemd 服务、Apache、Tomcat、PM2 Node.js、Git 部署、Certbot SSL、MinIO、GitLab Runner、WireGuard VPN、HAProxy、NTP 同步、Supervisor、NFS 挂载、Python venv、Go 二进制、Ansible Playbook、内核优化 |
| **backup 备份** | MySQL、PostgreSQL、MongoDB、Redis、Rsync |
| **diagnose 诊断** | Java CPU、SSL 证书检查、HTTP 健康检查、网络诊断、端口检测、内存诊断、进程监控、K8s 排查、Tcpdump 抓包 |
| **maintenance 维护** | 日志清理、磁盘清理、Docker 清理、ES 索引清理、Logrotate、Crontab 生成 |
| **security 安全** | Fail2ban、SSH 加固、UFW 防火墙、WireGuard VPN |

完整列表可通过 API 获取：`GET /api/templates`

### 模板搜索与分页

首页支持按名称/描述搜索、分类筛选、来源筛选（内置/自定义），以及分页浏览：

```
http://localhost:5001/?q=nginx&category=deploy&source=builtin&page=1&per_page=12
```

### 自定义模板

- Web 界面：首页点击 **「添加模板」**，或访问 `/templates/new`
- 自定义模板保存在 `data/custom_templates/` 目录
- 支持在线 YAML 编辑、验证、保存、删除
- 内置模板不可编辑/删除，可加载示例后修改另存

```bash
# API 创建自定义模板
curl -X POST http://localhost:5001/api/templates \
  -H "Content-Type: application/json" \
  -d '{"yaml_content":"name: hello\ntitle: Hello\nscript: |\n  echo hi\n"}'

# API 搜索模板
curl "http://localhost:5001/api/templates?q=backup&page=1&per_page=6"
```

## API 接口

### 获取模板列表

```bash
curl http://localhost:5000/api/templates
```

### 获取模板详情

```bash
curl http://localhost:5000/api/template/nginx
```

### 生成脚本

```bash
curl -X POST http://localhost:5000/api/generate \
  -H "Content-Type: application/json" \
  -d '{"template":"nginx","params":{"port":8080,"enable_https":true,"domain":"example.com","email":"admin@example.com"}}'
```

## 非交互模式

通过 URL 参数预填表单：

```
http://localhost:5000/template/nginx?port=8080&enable_https=true&domain=example.com
```

## 分享功能

生成脚本后自动创建分享链接：

```
http://localhost:5000/share/abc123def4
```

## 模板开发

在 `templates/` 目录下创建 YAML 文件：

```yaml
name: my_template
title: 我的模板
description: 模板描述
icon: 📜
category: general

questions:
  - name: param1
    label: 参数1
    type: string
    default: value
    required: true
  - name: param2
    label: 参数2
    type: bool
    default: false
    show_when:
      param1: value

computed:
  derived: "param1 + '_suffix'"

script: |
  #!/bin/bash
  echo "{{ param1 }}"
  echo "{{ derived }}"
```

### 支持的问题类型

- `string` - 字符串
- `int` - 整数（支持 min/max）
- `bool` - 布尔开关
- `choice` - 单选（需 choices）
- `multi` - 多选（需 choices）

### 条件显示

```yaml
show_when:
  enable_https: true
```

### 变量计算

```yaml
computed:
  ssl_port: "443 if enable_https else port"
  prefix: "domain.split('.')[0]"
```

## 项目结构

```
OpsGen/
├── app.py                 # Flask 主应用
├── engine/                # 脚本生成引擎
│   ├── loader.py          # YAML 模板加载
│   ├── generator.py       # Jinja2 脚本渲染
│   └── expressions.py     # 安全表达式求值
├── services/              # 历史记录 & 分享
├── templates/             # YAML 脚本模板
├── web_templates/         # Jinja2 HTML 页面
├── static/                # CSS & JS
└── data/                  # 运行时数据（历史、分享）
```

## 注意事项

- **在线执行**功能会在服务器本地运行 bash 脚本，生产环境请谨慎启用
- 生成的脚本仅供参考，部署前请审查并根据实际环境调整
- 密码等敏感信息不会持久化加密，请注意安全

## License

MIT
