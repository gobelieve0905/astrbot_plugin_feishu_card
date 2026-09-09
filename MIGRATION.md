# 独立开发交接

2026-09-09 从原 ads-analyze-agent 工作区迁出。现有源码、测试、配置 schema、metadata 和说明原样保留；生产中的插件未因源码迁出而卸载或重载。

本目录可作为新插件仓库的根目录。尚未创建新的 GitHub 仓库、提交或推送，原仓库的历史不会自动成为新仓库历史。

本机卡片测试：python3 -B -m unittest discover -s tests -p test_card.py。
运行测试需要 AstrBot 4.28.0 环境。独立运维工作区为 /Users/kingboat/Documents/server-operations/43.138.195.178；部署入口为 deploy-plugin.sh。
